"""_summary_

Returns:
    _description_
"""

import time
import glob
import os
import uuid
import logging
import threading
import callbacks
from speaker_metrics import speaker_metrics
from features_detector import features_detector
from features_detector import scorer_factory
from keyword_detector import keyword_detector
from doa.doa_respeaker_v2_6mic_array import calculateDOA
from speaker_diarization.pyDiarization import clusterSpectralEmbeddings
from speaker_diarization.pyDiarization import embedSignal, checkFingerprints, part_voice_features, reset_speaker_session_state
from speaker_diarization.pyDiarization import getSpectralEmbeddings
from speaker_diarization import segment_split
import numpy as np
from joblib import load
from topic_modeling.topic_modeling import preprocess_transcript
import config as cf
from processing_common import select_topic_id, save_embeddings, load_embeddings
from completion_latch import CompletionLatch
import segment_pool  # process-wide bounded utterance pool (audit B.4)
# from source_seperation import source_seperation_pre_trained
# from server.topic_modeling.topicmodeling import get_topics_with_prob
# For converting nano seconds to seconds.
NANO = 1000000000


class AudioProcessor:
    def __init__(self, audio_buffer, transcript_queue, diarization_model,
                 semantic_model, config):
        self.audio_buffer = audio_buffer
        self.transcript_queue = transcript_queue
        # Serializes appends on self.embeddings: per-utterance threads
        # appending while completion runs np.array(self.embeddings) raises
        # numpy's "content of sequences changed" (same fix as posthoc).
        self._embeddings_lock = threading.Lock()
        # Exactly-once completion accounting (running-count + asr-end + latch)
        # lives in CompletionLatch, a shared tested state machine — the live
        # and post-hoc processors used to hand-roll it and the copies drifted.
        self._latch = CompletionLatch(self.__complete_callback)
        self.mt_feats = np.array([])
        self.speakers = np.array([])
        self.signal = np.array([])
        self.max_speakers = 10
        self.embeddings = []
        self.embeddings_file = None
        self.diarization_model = diarization_model
        self.speaker_timings = []
        self.config = config
        self.fs = 16000
        self.running = False
        self.asr_complete = False
        self.topic_model = None
        self.fingerprints = None
        self.cohesion_window = 20
        # Stages that already logged a traceback this session (see _optional).
        self._stage_failures = set()

        self.semantic_model = semantic_model
        logging.info("Start metrics process")
        self.speaker_metrics_process = speaker_metrics.SpeakerProcessor(
            config, self.semantic_model)

        cf.initialize()

    def start(self):
        self.running = True
        self.asr_complete = False
        # Fresh latch per run (start may be called again to reprocess).
        self._latch = CompletionLatch(self.__complete_callback)
        self.processing_thread = threading.Thread(target=self.process)
        self.processing_thread.daemon = True
        if self.config.topic_model:
            logging.info("Loading Topic Model")
            self.topic_model = load(os.path.join(
                "topicModels", f'{self.config.owner}_{self.config.topic_model}'))
            logging.info("Loading successful")
        self.processing_thread.start()

    def stop(self):
        self.running = False

    def __complete_callback(self):
        logging.info("completing callback")
        '''
        self.speaker_transcript_queue.put(None)
        self.speaker_metrics_process.join()
        self.speaker_metrics_process.close()
        '''
        if self.config.diarization:
            try:
                self.send_speaker_taggings()
            except Exception as ex:
                logging.info(ex)
            results_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'speaker_diarization', 'results')
            os.makedirs(results_dir, exist_ok=True)
            np.savetxt(os.path.join(results_dir, "{}.txt".format(
                time.strftime("%Y%m%d-%H%M%S"))), self.speakers)
            # Debug artifacts: prune to the newest 200 — one file per
            # session accumulated here forever.
            try:
                for _old in sorted(glob.glob(os.path.join(results_dir, '*.txt')))[:-200]:
                    os.remove(_old)
            except OSError:
                pass

    def setSpeakerFingerprints(self, fingerprints):
        self.fingerprints = fingerprints
        logging.info("Set Speakers")
        # A fresh roster means a fresh session for these speakers: drop their
        # cross-session mic adaptation and re-read (possibly re-recorded)
        # enrollment prints from disk.
        try:
            reset_speaker_session_state(
                info.get('alias') for info in (fingerprints or {}).values())
        except Exception:
            logging.exception('resetting speaker session state failed')
        self.speaker_metrics_process.setSpeakers(self.fingerprints)

    def send_speaker_taggings(self):
        processing_timer = time.time()
        results = []
        if self.embeddings_file is not None and self.embeddings:
            try:
                save_embeddings(self.embeddings_file, self.embeddings)
            except Exception as e:
                logging.warning('could not save embeddings file %s: %s',
                                self.embeddings_file, e)
        spectralEmbeddings, n_speakers = getSpectralEmbeddings(self.embeddings)
        self.speakers, speaker_class_names, cls_ctrs = clusterSpectralEmbeddings(
            spectralEmbeddings, n_speakers, raw_list=self.embeddings)
        for i in range(0, len(self.speakers)):
            results.append({
                'speaker': 'Speaker {0}'.format(self.speakers[i]),
                'start': self.embeddings[i]['start'],
                'end': self.embeddings[i]['end']
            })

        # Convert results into expected JSON format.
        taggings = {}
        taggings["results"] = results

        processing_time = time.time() - processing_timer
        logging.info(taggings)  # DEBUG: Prints the converted speaker timings.
        taggings_posted = callbacks.post_tagging(
            self.config.auth_key, taggings, self.embeddings_file)
        if taggings_posted:
            logging.info('Tagging results posted successfully for  {0} (Processing time: {1})'.format(
                self.config.auth_key, processing_time))
        else:
            logging.info('Processing results FAILED to post for tagging {0} '.format(
                self.config.auth_key))

    def float_to_timestamp(self, t):
        hours = int(t / 3600)
        minutes = int((t % 3600) / 60)
        seconds = int((t % 60))
        milliseconds = int((t % 1) * 1000)
        return '{0}:{1}:{2}:{3}'.format(str(hours).zfill(2), str(minutes).zfill(2), str(seconds).zfill(2), str(milliseconds).zfill(4))

    def process(self):
        logging.info('Processing thread started for {0}.'.format(
            self.config.auth_key))
        self.embeddings_file = self.config.embeddings_file
        while not self.asr_complete:
            transcript_data = self.transcript_queue.get()
            if transcript_data is None:
                self.asr_complete = True
            else:
                # Gather audio data related to the transcript.
                words = transcript_data.alternatives[0].words
                start_time = words[0].start_time.seconds + \
                    (words[0].start_time.nanos / NANO)
                end_time = words[-1].end_time.seconds + \
                    (words[-1].end_time.nanos / NANO)
                transcript_audio_data = self.audio_buffer.extract(
                    start_time, end_time)
                # DoA, keywords, features etc. run on the process-wide
                # bounded pool (was an uncapped thread per segment). submit
                # never blocks; under backlog the oldest waiting segment is
                # degraded to lite (transcript still posted, extras skipped).
                self._latch.task_started()
                segment_pool.shared().submit(
                    self.process_transcript,
                    transcript_data, transcript_audio_data, start_time, end_time)
        self._latch.mark_asr_complete()
        logging.info('Processing thread stopped for {0}.'.format(
            self.config.auth_key))

    def _optional(self, stage, fn, default=None):
        # Enrichment stages are optional: one failing (a CUDA OOM in keyword
        # embedding, say) used to take the whole utterance with it because
        # everything shared one try. Log, skip, and post the transcript
        # anyway. Traceback once per stage per session; later repeats are a
        # single line so a persistently broken stage can't flood the log.
        try:
            return fn()
        except Exception as e:
            first = stage not in self._stage_failures
            self._stage_failures.add(stage)
            logging.warning('%s stage failed for %s (%s); transcript continues without it',
                            stage, self.config.auth_key, e, exc_info=first)
            return default

    def _enrich(self, text, lite=False):
        """Questions / keywords / features for one transcript row, each optional."""
        questions = keywords = features = None
        if lite:
            return questions, keywords, features
        if self.config.transcribe:
            questions = self._optional('questions', lambda: features_detector.detect_questions(text))
        if self.config.keywords:
            keywords = self._optional('keywords', lambda: keyword_detector.detect_keywords(text, self.config.keywords))
        if self.config.features:
            features = self._optional('features', lambda: scorer_factory.get_scorer(cf.scorer()).detect_features(text))
        return questions, keywords, features

    def _topic_id(self, transcript_text, default=-1):
        logging.info("Text for topic modeling")
        logging.info(transcript_text)
        preprocessed = preprocess_transcript(transcript_text, [""])
        logging.info("Preprocessed")
        logging.info(preprocessed)
        logging.info(self.topic_model.id2word)
        text2bow = self.topic_model.id2word.doc2bow(preprocessed)
        logging.info("Corpus")
        logging.info(text2bow)
        topic_id = default
        if len(text2bow):
            topics = self.topic_model[text2bow]
            logging.info("Topics distribution: ")
            logging.info(topics)
            #    topics = get_topics_with_prob(transcript_text)
            # Shared argmax (processing_common) — was duplicated and
            # drifting between the live and posthoc processors.
            topic_id = select_topic_id(topics, default=topic_id)
        logging.info(topic_id)
        return topic_id

    # Processes a transcript and its related audio data. ``lite`` (set by the
    # pool under backlog) skips every optional stage except one whole-segment
    # speaker match; the transcript is always posted.
    def process_transcript(self, transcript_data, audio_data, start_time, end_time, lite=False):
        try:
            processing_timer = time.time()
            words = transcript_data.alternatives[0].words
            # Get Transcript, Questions, Keywords and Features.
            transcript_text = None
            if self.config.transcribe:
                transcript_text = transcript_data.alternatives[0].transcript
            questions, keywords, features = self._enrich(transcript_text, lite)

            # Get Topics
            topic_id = -1
            if self.topic_model and not lite:
                topic_id = self._optional('topic', lambda: self._topic_id(transcript_text), default=-1)

            # Get DoA (Direction of Arrival)
            doa = None
            if self.config.doa and self.config.channels == 6 and not lite:
                word_timings = [(word.start_time.seconds + (word.start_time.nanos / NANO),
                                 word.end_time.seconds + (word.end_time.nanos / NANO)) for word in words]
                doa = self._optional('doa', lambda: calculateDOA(
                    start_time, audio_data, word_timings,
                    16000, self.config.channels, self.config.depth))

            start_time += self.config.start_offset
            end_time += self.config.start_offset

            # Perform Speaker Diarization (interruption-aware: a segment where
            # the speaker flips mid-way is split at the change point into two
            # cleanly-attributed rows; true overlap is tagged contested).
            speaker_tag = None
            speaker_id = -1
            if self.config.diarization and self.fingerprints and len(self.fingerprints):
                word_tuples = [
                    (w.word,
                     w.start_time.seconds + (w.start_time.nanos / NANO) + self.config.start_offset,
                     w.end_time.seconds + (w.end_time.nanos / NANO) + self.config.start_offset)
                    for w in words]
                parts = None
                if not lite:  # lite: one encode instead of ~30 windowed ones
                    try:
                        parts = segment_split.split_and_attribute(
                            np.frombuffer(audio_data, dtype=np.int16), word_tuples,
                            start_time, end_time, self.fingerprints,
                            self.diarization_model)
                    except Exception as e:
                        logging.warning('segment split failed (%s); whole-segment matching', e)
                if parts is None:
                    # Unattributed rather than lost if matching fails too.
                    tag, sid, conf = self._optional(
                        'fingerprint match',
                        lambda: checkFingerprints(audio_data, self.fingerprints, self.diarization_model),
                        default=(None, -1, None))
                    parts = [{'start': start_time, 'end': end_time,
                              'text_slice': (0, len(words)), 'alias': tag,
                              'speaker_id': sid, 'contested': None,
                              'confidence': conf}]
                multi = len(parts) > 1
                for p in parts:
                    if multi:
                        w0, w1 = p['text_slice']
                        p_text = ' '.join(word_tuples[i][0] for i in range(w0, w1)).strip() or transcript_text
                        p_questions, p_keywords, p_features = self._enrich(p_text, lite)
                    else:
                        p_text, p_questions, p_keywords, p_features = transcript_text, questions, keywords, features
                    self.speaker_metrics_process.process_transcript(
                        {
                            'source': self.config.auth_key,
                            'start_time': p['start'],
                            'end_time': p['end'],
                            'transcript': p_text,
                            'doa': doa,
                            'questions': p_questions,
                            'keywords': p_keywords,
                            'features': p_features,
                            'topic_id': topic_id,
                            'speaker_tag': p['alias'],
                            'speaker_id': p['speaker_id'],
                            'voice_features': part_voice_features(p),
                        })
            else:
                if self.config.diarization:
                    if len(self.embeddings) == 0 and self.embeddings_file is not None:
                        try:
                            # Shared loader (processing_common): object array,
                            # allow_pickle required, or resume restarts from [].
                            self.embeddings = load_embeddings(self.embeddings_file)
                        except Exception as e:
                            logging.error(
                                "Unable to load embeddings file: %s", e)
                            self.embeddings = []
                    elif self.embeddings_file is None:
                        # Absolute path + auth_key + random suffix: the old
                        # CWD-relative second-granular name collided when two
                        # pods started the same second.
                        emb_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'embeddings')
                        os.makedirs(emb_dir, exist_ok=True)
                        self.embeddings_file = os.path.join(
                            emb_dir, '{0}-{1}.npy'.format(self.config.auth_key, uuid.uuid4().hex[:8]))
                    embedding = self._optional(
                        'speaker embedding', lambda: embedSignal(audio_data, self.diarization_model))
                    # Saved once at completion (send_speaker_taggings) — the
                    # old per-utterance re-save of the whole array was O(n²)
                    # disk writes and raced concurrent appends.
                    if embedding is not None:
                        with self._embeddings_lock:
                            self.embeddings.append({
                                'embedding': embedding,
                                'start': start_time,
                                'end': end_time,
                            })
                success, transcript_id = callbacks.post_transcripts(
                    self.config.auth_key, start_time, end_time,
                    transcript_text, doa, questions, keywords,
                    features, topic_id, speaker_tag, speaker_id)
                
                processing_time = time.time() - processing_timer

                if success:
                    logging.info( f"Processing results posted successfully for client {self.config.auth_key} (Processing time: {processing_time}) @ {start_time} for transcript {transcript_id}")
                else:
                    logging.warning("Processing results FAILED to post for"
                                    " client %s (Processing time: %.2f)",
                                    self.config.auth_key, processing_time)

            # Get source seperation
            # if self.config.source_seperation:
            #   source_seperation = source_seperation_pre_trained(audio_data)

        except Exception:
            # %d with a string auth_key made the logging call itself raise,
            # replacing the real error with a "--- Logging error ---" dump
            # (posthoc got this fix earlier; the live side never did).
            logging.exception("Processing FAILED for client %s",
                              self.config.auth_key)

        # Check if this was the final process of the transmission.
        self._latch.task_done()

       
