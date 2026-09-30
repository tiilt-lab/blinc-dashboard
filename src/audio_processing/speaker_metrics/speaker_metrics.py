import numpy as np
import threading
import logging
import time
import callbacks

'''
SpeakerProcessor

Perfrom textual analysis of each speakers data using the transcripts and diarization output.

Implements a modified version of the Group Communication Analysis (GCA) framework:

    Dowell, N. M. M., Nixon, T. M., & Graesser, A. C. (2019).
    Group communication analysis: A computational linguistics approach for
    detecting sociocognitive roles in multiparty interactions.
    Behavior Research Methods, 51(3), 1007-1041.
    https://doi.org/10.3758/s13428-018-1102-z

The variable names track the paper's notation: tau_window is the lag window
tau, xi_sums accumulates the cross-cohesion terms xi. From the cross-cohesion
tensor come the responsivity matrix and its derived per-speaker measures
(internal cohesion, social impact, overall responsivity); newness and
communication density follow the paper's LSA-subspace formulation, computed
here with sentence-transformer embeddings. Matrix operations keep the update
incremental as the discussion continues.

These scores surface in the frontend as "Participation and Impact Style".
'''

normalizeVector = lambda v: np.divide(v, np.linalg.norm(v))
projection = lambda v1, v2 : np.multiply(np.dot(v2, v1)/np.dot(v1, v1), v1)

class SpeakerProcessor:

    speakers = None
    participants = 0
    indicies = None

    def __init__(self, config, semantic_model, usedby="audio_processor"):

        logging.info("[Speaker_Metrics]Inside speaker metric init")
        self.total_contributions = 0
        self.asr_complete = False
        self.auth_key = config.auth_key
        logging.info(self.auth_key)
        logging.info('[Speaker_Metrics]Loaded semenatic model')

        # Which pipeline created this processor (live audio vs post-hoc speaker
        # metric recomputation); accepted for compatibility with the post-hoc
        # SpeakerMetricProcessor.
        self.usedby = usedby
        # The metric math below is stateful and order-dependent; post-hoc
        # processing calls process_transcript from one thread per utterance
        # (all at once for batch ASRs), so serialize it.
        self.process_lock = threading.Lock()
        self.running_process = None
        self.running = False

        self.embeddings = np.array([])
        self.subspace_basis = np.array([])
        self.tau_window = 20

        self.length = 0
        self.semantic_model = semantic_model

    def setSpeakers(self, speakers):
      if speakers is None:
         return
      self.speakers = speakers
      self.participants = len(speakers)
      self.indicies = {k: i for i ,k in enumerate(speakers)}

      self.contributions = np.zeros(self.participants+1, dtype=int)
      self.prev_window_speakers =  []
      self.window_lagged_contributions = np.zeros((self.tau_window, self.participants+1, self.participants+1), dtype=int)
      self.xi_sums = np.zeros((self.tau_window, self.participants+1, self.participants+1), dtype=float)
      self.resp_vals = np.zeros((self.participants+1, self.participants+1), dtype=float)
      self.total_new = np.zeros(self.participants+1, dtype=float)

      self.ignore_diag = np.ones((self.participants+1, self.participants+1), dtype=bool)
      np.fill_diagonal(self.ignore_diag, 0)

      self.participation_scores = np.zeros((self.participants+1), dtype=float)
      self.internal_cohesion = np.zeros((self.participants+1), dtype=float)
      self.overall_responsivity = np.zeros((self.participants+1), dtype=float)
      self.social_impact = np.zeros((self.participants+1), dtype=float)
      self.newness = self.total_new
      self.communication_density = np.zeros((self.participants+1), dtype=float)

    def calculateCohesionSums(self, speaker, embedding, model):
      if self.length >= self.tau_window:
        for i in range(0, self.tau_window):
            prev_speaker =  self.indicies[self.prev_window_speakers[i]] + 1 if self.prev_window_speakers[i] != -1 else 0
            self.xi_sums[i][prev_speaker][speaker] +=  model.similarity(self.embeddings[self.length-i-1], embedding)
            self.window_lagged_contributions[i][prev_speaker][speaker] += 1
        self.prev_window_speakers.pop(0)
      else:
         for i in range(0, self.length):
            prev_speaker = self.indicies[self.prev_window_speakers[i]] + 1 if self.prev_window_speakers[i] != -1 else 0
            self.xi_sums[i][prev_speaker][speaker] += model.similarity(self.embeddings[self.length-i-1], embedding)
            self.window_lagged_contributions[i][prev_speaker][speaker] += 1

      with np.errstate(divide='ignore', invalid='ignore'):
          cross_cohesion = np.divide(self.xi_sums, self.window_lagged_contributions)
          cross_cohesion = np.nan_to_num(cross_cohesion)

      return cross_cohesion

    def subspaceProjection(self, s, v):
        proj = 0
        for vector in s:
            proj += projection(vector, v)
        return proj

    def processResponsivity(self, cross_cohesion):
        responsivity = np.divide(np.sum(cross_cohesion, axis=0), self.tau_window)
        self.internal_cohesion = np.diagonal(responsivity)
        self.social_impact = np.divide(np.sum(responsivity, axis=0, where=self.ignore_diag), self.participants)
        self.overall_responsivity = np.divide(np.sum(responsivity, axis=1, where=self.ignore_diag), self.participants)

    def calculateNewness(self, embedding, speaker):
        self.embeddings = np.concatenate((self.embeddings, np.array([embedding])))
        given_data = self.subspaceProjection(self.subspace_basis, embedding)
        new_data = np.array([embedding - given_data])
        new_norm = np.linalg.norm(new_data)
        given_norm = np.linalg.norm(given_data)
        # A near-duplicate utterance (repeated "yeah"s) lies inside the
        # accumulated subspace: new_norm ~ 0, and normalizing that residual
        # put a NaN row into subspace_basis, silently zeroing newness for
        # every later utterance of the session. Contribute nothing new, and
        # keep the basis clean.
        if new_norm > 1e-9:
            self.total_new[speaker] += new_norm / (given_norm + new_norm)
            normalized_new_data = normalizeVector(new_data)
            self.subspace_basis = np.concatenate((self.subspace_basis, normalized_new_data), axis = 0)
        with np.errstate(divide='ignore', invalid='ignore'):
            self.newness = np.divide(self.total_new, self.contributions)
            self.newness = np.nan_to_num(self.newness)

    def start(self):
        self.running = True
        self.asr_complete = False

    def stop(self):
        self.running = False
        self.asr_complete = True

    def process_transcript(self, speaker_transcript_data, action="realtime_processing"):
      try:
       with self.process_lock:
        processing_timer = time.time()
        speaker_ids = list(self.speakers.keys())

        speaker = speaker_transcript_data['speaker_id']
        transcript = speaker_transcript_data['transcript']
        index = self.indicies[speaker] + 1 if speaker != -1 else 0
        self.contributions[index] += 1

        # Cohesion, responsivity, social impact, and newness each measure an
        # utterance against the REST of the discussion, so their value is only
        # meaningful once the whole session exists — and the sentence-embedding
        # + subspace math is the expensive part of this pipeline. They are a
        # post-hoc concern: the post-hoc pass rebuilds this state from scratch
        # over every stored transcript. Live computes participation only, which
        # keeps the in-class dashboard responsive and frees GPU for ASR and
        # diarization. (The semantic-metric arrays stay at their zero init and
        # are overwritten when post-hoc runs.) Every non-realtime caller — the
        # post-hoc re-analysis and the "pi" metric recomputation — still
        # computes the full set.
        if action != "realtime_processing":
          embedding = self.semantic_model.encode(transcript)
          if self.length > 0:
            cross_cohesion = self.calculateCohesionSums(index, embedding, self.semantic_model)
            self.processResponsivity(cross_cohesion)
            self.calculateNewness(embedding, index)
          else:
            self.embeddings = np.array([embedding])
            self.subspace_basis = normalizeVector(self.embeddings)
            self.total_new[index] += 1

        self.length += 1
        self.prev_window_speakers.append(speaker)
        self.participation_scores = np.subtract(np.multiply(np.divide(self.contributions, self.length), self.participants), 1)

        processing_time =  time.time() - processing_timer
        # Snapshot under the lock; the post below is a 30s-timeout HTTP call
        # and holding process_lock across it stalled every other utterance.
        metrics = (speaker_ids,
                   self.participation_scores.tolist(),
                   self.internal_cohesion.tolist(),
                   self.overall_responsivity.tolist(),
                   self.social_impact.tolist(),
                   self.newness.tolist(),
                   self.communication_density.tolist())

       success = callbacks.post_speaker_transcript_metrics(speaker_transcript_data, *metrics)

       if success:
          logging.info('[Speaker_Metrics]Processing posted successfully for client {0} (Processing time: {1}) @ {2}'.format(self.auth_key, processing_time, processing_timer))
       else:
          logging.warning('[Speaker_Metrics]Processing results FAILED to post for client {0} (Processing time: {1})'.format(self.auth_key, processing_time))
      except Exception as e:
         logging.error('[Speaker Metrics]Processing FAILED for client {0}: {1}'.format(self.auth_key, e))
