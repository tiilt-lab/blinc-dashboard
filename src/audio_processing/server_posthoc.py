import os as _rs_os, sys as _rs_sys  # noqa: E401
_rs_c = _rs_os.path.dirname(_rs_os.path.abspath(__file__))
while _rs_c != '/' and not _rs_os.path.isdir(_rs_os.path.join(_rs_c, 'common')):
    _rs_c = _rs_os.path.dirname(_rs_c)
_rs_c = _rs_os.path.join(_rs_c, 'common')
if _rs_c not in _rs_sys.path:
    _rs_sys.path.insert(0, _rs_c)
import reactor_safety  # reactor/thread boundary; src/common bootstrapped above
from ws_protocol import WsMessageMixin  # shared onMessage/onClose
from recording_filename import parse_recording_filename  # key/offset from filename
import os
import time
import logging
import threading
import wave
import config as cf
import callbacks
import numpy as np
from queue import Queue
from pathlib import Path
from scipy.io import wavfile
from processor_posthoc import AudioProcessorPosthoc
from processor_speaker_metric import SpeakerMetricProcessor
from processing_config import ProcessingConfig
from connection_manager import ConnectionManager  # also puts src/common on sys.path
import audio_bytes
import safe_names
from audio_buffer import AudioBuffer
from audio_stream_reader import AudioStreamReader
from asr_connectors.google_asr_connector_posthoc import GoogleASR
from twisted.internet import reactor, task
from features_detector import features_detector
from keyword_detector import keyword_detector
from autobahn.twisted.websocket import WebSocketServerFactory
from autobahn.twisted.websocket import WebSocketServerProtocol
from speechbrain.inference import SpeakerRecognition
from sentence_transformers import SentenceTransformer

cm = ConnectionManager()
semantic_model = SentenceTransformer("all-mpnet-base-v2")

# P&I semantic-cohesion embedder, selectable per run. bge-large-en-v1.5 (open
# SOTA sentence embedder) is the default; mpnet kept for comparability with
# historical metrics. Loaded lazily, cached for the server lifetime.
_EMBEDDERS = {"all-mpnet-base-v2": semantic_model}
_EMBEDDER_IDS = {
    "bge-large-en-v1.5": "BAAI/bge-large-en-v1.5",
    "all-mpnet-base-v2": "all-mpnet-base-v2",
}

def get_semantic_model(name=None):
    key = (name or "bge-large-en-v1.5").strip()
    if key not in _EMBEDDER_IDS:
        logging.warning("Unknown embedder '%s'; using bge-large-en-v1.5", name)
        key = "bge-large-en-v1.5"
    if key not in _EMBEDDERS:
        logging.info("Loading semantic embedder %s", _EMBEDDER_IDS[key])
        _EMBEDDERS[key] = SentenceTransformer(_EMBEDDER_IDS[key])
    return _EMBEDDERS[key]
diarization_model = SpeakerRecognition.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb", savedir="pretrained_models/pretrained_ecapa")
semantic_model.share_memory()
STOP_SIGNAL = object()

# Cross-process registry of pods currently under posthoc processing. Fuses a
# Redis claim (at most one run per pod across ALL processes; TTL self-heals a
# dead claim) with the local dict that holds the live processor object for
# teardown. Drop-in for the old dict where the processors touch it
# (pop/get/keys/[]=); the claim decision is the atomic try_claim below.
import redis_client
from pod_registry import PodRegistry
running_audio_processes = PodRegistry(redis_factory=redis_client._redis,
                                      prefix='audio-posthoc:')
# Kept only for the few multi-op sections that snapshot-then-mutate; the
# registry is internally locked, so single ops don't need it.
_running_guard = threading.Lock()

class ServerProtocol(WsMessageMixin, WebSocketServerProtocol):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_message = time.time()
        self.stream_data = False
        self.interval = 10
        # Only the full-audio-analytics path (signal_start) creates these; the
        # transcript-only P&I / E&T operations never do, so default them so
        # signal_end / onClose cleanup doesn't crash on an unset attribute.
        self.asr = None
        self.processor = None
        self.audioreader = None
        self.processorspeakermetric = None
        self.config = None
        # Consulted by the connection manager's periodic key check; posthoc
        # connections aren't live-key gated, so leave it false.
        self.running = False
        # True while a full-audio run is processing. When set, signal_end (from
        # a client disconnect or heartbeat timeout) leaves the processors running
        # so the run finishes in the background and marks itself complete.
        self._run_active = False

        cm.add(self)
        logging.info('New client connected...')

    # onMessage / onClose come from WsMessageMixin (shared across both services).

    def process_json(self, data):
        if not 'type' in data:
            logging.warning('Message does not contain "type".')
            return
        
        if data['type'] == 'Initialize_audio_processing_analytics':
            self.sessionid = data['sessionid']
            self.session_device_id = data['sessiondeviceid']
            self.server_start= data['server_start']
            self.keywords= data['keywords']
            # Optional per-run model choices from the trigger UI; fall back to
            # the deployment config when absent.
            self.asr_choice = data.get('asr') or cf.asr()
            self.scorer_choice = data.get('scorer') or None
            self.diarizer_choice = data.get('diarizer') or 'fingerprint'
            self.embedder_choice = data.get('embedder') or None
            # Optional per-analysis toggles — default to running everything, so
            # omitting them reproduces the previous behaviour. Lets a re-run skip
            # expensive stages (e.g. re-score features without re-running DOA).
            self.run_transcribe = data.get('run_transcribe', True)
            self.run_features = data.get('run_features', True)
            self.run_doa = data.get('run_doa', True)
            self.run_tagging = data.get('run_tagging', True)
            self.topic_model_choice = data.get('topic_model', None)
            # Per-run model provenance (persisted on completion, see #5).
            self.model_choices = {
                'asr': self.asr_choice, 'scorer': self.scorer_choice or cf.scorer(),
                'diarizer': self.diarizer_choice,
                'embedder': self.embedder_choice or 'bge-large-en-v1.5',
                'keyword_backend': cf.keyword_backend(),
                'diarization_fallback': cf.diarization_fallback(),
            }
            self.speakers = dict()
            self.stream_data = "audio"
            self.buffer_size = 4096
            self.sample_rate = 16000
            self.queue_put_timeout = 0.05
            self.audio_file = self.get_audio_file_path(self.session_device_id)

            if not self.audio_file:
                self.send_json({'type': 'error', 'message': 'No audio captured for this group.'})
            elif os.path.getsize(str(self.audio_file)) < 10000 and not self._recover_audio_from_video():
                # Header-only stub wav AND no recoverable audio track in the
                # pod's video -> fail loudly (a silent zero-transcript
                # "analysis" is worse than an error).
                self.send_json({'type': 'error', 'message': 'Audio recording is empty ({0} bytes) and no audio track could be recovered from the video.'.format(os.path.getsize(str(self.audio_file)))})
            else:
                self.audio_file = str(self.audio_file)
                # key IS the pod processing_key — the source every result
                # callback is matched on server-side. The "<key> (ts)..." form
                # used to leak a trailing space into the key, so the source no
                # longer matched session_device.processing_key and every DB
                # callback silently no-oped. See tests/test_recording_filename.py.
                key, off_set_date = parse_recording_filename(self.audio_file)
 
                 #keep track of currently running posthoc audio analytics
                # Atomic cross-process claim; False if a run for this pod is
                # already active anywhere. registry.pop(...) later releases it.
                if not running_audio_processes.try_claim(key):
                    self.send_json({'type': 'error', 'message': 'Audio posthoc analytics for this group is already running'})
                    return

                # Anything that throws between claiming the registry entry and
                # signal_start used to leak the claim (swallowed by onMessage's
                # catch-all), leaving the pod "already running" until restart.
                try:
                    conf_val = {'key':key,'encoding': "pcm_f32le", 'sample_rate': self.sample_rate,'channels': 1,'sessionid': self.sessionid,'deviceid': self.session_device_id,
                                'tag': self.run_tagging, 'server_start':self.server_start,'keywords':self.keywords,'transcribe':self.run_transcribe,'features':self.run_features,'doa':self.run_doa,'topic_model':self.topic_model_choice,'owner':1,'off_set_date':off_set_date}
                    valid, result = ProcessingConfig.from_json(conf_val,source="posthoc processing")
                    if not valid:
                        # registry.pop releases the cross-process claim too.
                        running_audio_processes.pop(key, None)
                        logging.info("Configuration setting failed for audio posthoc processing")
                    else:
                        self.config = result

                        if os.path.splitext(self.audio_file)[1] == ".dat":
                            wavfilename =  os.path.splitext(self.audio_file)[0]+".wav"
                            with open(self.audio_file, "rb") as f:
                                all_data = f.read()
                                all_data = np.frombuffer(all_data, np.float32 if self.config.depth == 4 else np.int16, -1)
                                chunk_length = int(len(all_data) / self.config.channels)
                                all_data = np.reshape(all_data, (chunk_length, self.config.channels))
                            wavfile.write(wavfilename, self.config.sample_rate, all_data)
                            os.remove(self.audio_file)
                            self.audio_file = wavfilename


                        #start processing
                        for speaker in data['speakers']:
                            try:
                                alias = safe_names.safe_name(speaker["alias"])
                            except safe_names.UnsafeName:
                                logging.warning('skipping speaker with unsafe alias %r', speaker.get("alias"))
                                continue
                            audio_fingerprint_file = os.path.join(cf.biometric_folder(), "{0}".format(alias))
                            try:
                                with wave.open(audio_fingerprint_file+'.wav') as wavObj:
                                    byte_audio_data = self.read_bytes_from_wav(wavObj)
                            except (FileNotFoundError, wave.Error) as e:
                                # One roster member without a usable enrollment
                                # recording must not abort the whole run.
                                logging.warning('skipping speaker %r: unreadable enrollment wav (%s)',
                                                speaker.get("alias"), e)
                                continue
                            self.speakers[speaker["id"]] = {"alias": speaker["alias"], "data": byte_audio_data}

                        self.signal_start()
                        self.processor.setSpeakerFingerprints(self.speakers)

                        self.send_json({'type':'init posthoc analytics completed','message':"Starting Audio Analytics Processing"})
                        logging.info('Audio Posthoc analytics initiated')
                except Exception:
                    running_audio_processes.pop(key, None)
                    logging.exception('posthoc init failed after claiming %s; claim released', key)
                    self.send_json({'type': 'error', 'message': 'Post-hoc initialization failed; see the audio service log.'})

        if data['type'] == 'Initialize_participation_and_impact_style_computation':
            self.sessionid = data['sessionid']
            self.session_device_id = data['sessiondeviceid']
            self.server_start= data['server_start']
            self.keywords= data['keywords'] 
            self.transcript = data["transcript"]
            self.sample_rate = 16000
            self.speakers = dict()

            self.audio_file = self.get_audio_file_path(self.session_device_id)

            if self.audio_file:
                self.audio_file = str(self.audio_file)
                # key IS the pod processing_key — the source every result
                # callback is matched on server-side. The "<key> (ts)..." form
                # used to leak a trailing space into the key, so the source no
                # longer matched session_device.processing_key and every DB
                # callback silently no-oped. See tests/test_recording_filename.py.
                key, off_set_date = parse_recording_filename(self.audio_file)
            else:
                key = "No key"
                off_set_date = "Sat Jun 27 18:17:13 2026" #this is just a generic date

                #keep track of currently running posthoc audio analytics
            if not running_audio_processes.try_claim(key):
                self.send_json({'type': 'error', 'message': 'Audio posthoc analytics for this group is already running'})
                return

            conf_val = {'key':key,'encoding': "pcm_f32le", 'sample_rate': self.sample_rate,'channels': 1,'sessionid': self.sessionid,'deviceid': self.session_device_id,
                            'tag': True, 'server_start':self.server_start,'keywords':self.keywords,'transcribe':True,'features':True,'doa':True,'topic_model':None,'owner':1,'off_set_date':off_set_date}
            valid, result = ProcessingConfig.from_json(conf_val,source="posthoc processing")
            if not valid:
                # Release the claim or this pod is blocked until restart.
                running_audio_processes.pop(key, None)
                logging.info("Configuration setting failed for audio posthoc processing")
            else:    
                self.config = result
                for speaker in data['speakers']:
                    self.speakers[speaker["id"]] = {"alias": speaker["alias"], "id": speaker["id"]}
                self.processorspeakermetric = SpeakerMetricProcessor(self.sessionid, self.session_device_id, self.transcript,get_semantic_model(data.get('embedder')),self.config,running_audio_processes, scorer=data.get('scorer'))
                self.processorspeakermetric.setSpeakerFingerprints(self.speakers)
                self.send_json({'type':'init participation and impact style completed','message':"Starting Speaker Transcript Metric Processing"})
                logging.info('participation and impact style initiated')

        if data['type'] == 'Initialize_expressing_and_thinking_style_computation':
            self.sessionid = data['sessionid']
            self.session_device_id = data['sessiondeviceid']
            self.server_start= data['server_start']
            self.keywords= data['keywords'] 
            self.transcript = data["transcript"]
            self.sample_rate = 16000
            self.speakers = dict()

            self.audio_file = self.get_audio_file_path(self.session_device_id)

            if not self.audio_file:
                self.send_json({'type': 'error', 'message': 'No audio captured for this group.'})
            else:
                self.audio_file = str(self.audio_file)
                # key IS the pod processing_key — the source every result
                # callback is matched on server-side. The "<key> (ts)..." form
                # used to leak a trailing space into the key, so the source no
                # longer matched session_device.processing_key and every DB
                # callback silently no-oped. See tests/test_recording_filename.py.
                key, off_set_date = parse_recording_filename(self.audio_file)
 
                 #keep track of currently running posthoc audio analytics
                if not running_audio_processes.try_claim(key):
                    self.send_json({'type': 'error', 'message': 'Audio posthoc analytics for this group is already running'})
                    return

                conf_val = {'key':key,'encoding': "pcm_f32le", 'sample_rate': self.sample_rate,'channels': 1,'sessionid': self.sessionid,'deviceid': self.session_device_id,
                                'tag': True, 'server_start':self.server_start,'keywords':self.keywords,'transcribe':True,'features':True,'doa':True,'topic_model':None,'owner':1,'off_set_date':off_set_date}
                valid, result = ProcessingConfig.from_json(conf_val,source="posthoc processing")
                if not valid:
                    # Release the claim or this pod is blocked until restart.
                    running_audio_processes.pop(key, None)
                    logging.info("Configuration setting failed for audio posthoc processing")
                else:    
                    self.config = result
                    for speaker in data['speakers']:
                        self.speakers[speaker["id"]] = {"alias": speaker["alias"], "id": speaker["id"]}
                    self.processorspeakermetric = SpeakerMetricProcessor(self.sessionid, self.session_device_id, self.transcript,get_semantic_model(data.get('embedder')),self.config,running_audio_processes, scorer=data.get('scorer'))
                    self.processorspeakermetric.setSpeakerFingerprints(self.speakers)
                    self.send_json({'type':'init expression and thinking style completed','message':"Starting Transcript Metric Processing"})
                    logging.info('Expression and thinking style initiated')

        if data['type'] == 'start_posthoc_audio_processing':
            # start reading audio from wav file and pass to processors
            self.send_json({'type':'audio posthoc analytics started','message':"Processing Audio posthoc Analytics"})
            self.audioreader.add_websocket_connection(self)
            self.processor.add_websocket_connection(self)
            self.audioreader.start()

        if data['type'] == 'start_speaker_transcript_processing':
            self.send_json({'type':'speaker metric computation started','message':"Computing Speaker Metric"})
            self.processorspeakermetric.add_websocket_connection(self)
            self.processorspeakermetric.start_speaker_metric_processing()
        if data['type'] == 'start_transcript_metric_processing':
            self.send_json({'type':'transcript metric computation started','message':"Computing Transcript Metric"})
            self.processorspeakermetric.add_websocket_connection(self)
            self.processorspeakermetric.start_transcript_metric_processing()    

        if data['type'] == 'cancel_posthoc':
            sdid = str(data.get('sessiondeviceid', ''))
            with _running_guard:
                hits = [k for k in list(running_audio_processes.keys()) if k.startswith(sdid + '-')]
            for k in hits:
                proc = running_audio_processes.pop(k, None)
                if hasattr(proc, 'stop'):
                    try:
                        proc.stop()
                    except Exception:
                        pass
            self.send_json({'type': 'posthoc_cancelled', 'cancelled': len(hits)})

        if data['type'] == 'query_posthoc_status':
            # Is a run for this pod still active (possibly started by another
            # page/session)? Lets the UI restore progress after a refresh.
            sdid = str(data.get('sessiondeviceid', ''))
            match = next((k for k in list(running_audio_processes.keys())
                          if k.startswith(sdid + '-')), None)
            self.send_json({'type': 'posthoc_status', 'running': match is not None})
            if match is not None:
                # Re-attach the running processor to THIS socket so live
                # progress / completion stream to the reconnected client, and
                # adopt the run so completion bookkeeping works from here.
                proc = running_audio_processes.get(match)
                if hasattr(proc, 'add_websocket_connection'):
                    proc.add_websocket_connection(self)
                    self.config = getattr(proc, 'config', None) or self.config
                    self._run_active = True

        if data['type'] == 'heartbeat_from_posthoc_processing':
            auth_key = data.get('key', None)
            logging.info("Recieved Heartbeat from client with authkey {0}".format(auth_key))   




    def get_audio_file_path(self,sessionDeviceId):
        current_dir = Path(__file__).resolve().parent
        # Go up one level (parent)
        parent_dir = current_dir.parent
        # Go into sibling directory
        target_dir = parent_dir / "audio_processing" / "recordings"
        # Coerce to int: the id comes off the socket untyped, and it's used as
        # a glob prefix. int() blocks path-traversal patterns; the trailing "-"
        # stops device 9 matching 91-/918- (recording names are "<id>-<uuid>...").
        try:
            prefix = str(int(sessionDeviceId))
        except (TypeError, ValueError):
            logging.warning("get_audio_file_path: non-integer device id %r", sessionDeviceId)
            return None
        files = list(target_dir.glob("{0}-*".format(prefix)))

        if files:
            file_path = files[0]
            logging.info("Found: {0}".format(file_path))
            return file_path
        else:
            logging.info("No file found")
            return None

    def read_bytes_from_wav(self,wav):
        return audio_bytes.read_bytes_from_wav(wav)

    def send_json(self, message):
        # Reactor-safe transport write (was a raw sendMessage from posthoc
        # worker threads). See common/reactor_safety.
        reactor_safety.send_json(self, message)

    def send_close(self, message):
        self.send_json({'type': 'end', 'message': message})

    def signal_start(self):
        # WhisperX transcribes the whole file before emitting results, so the
        # buffer must retain the entire recording (2h cap ≈ 230MB PCM).
        buffer_seconds = 7200 if getattr(self, 'asr_choice', None) in ('whisperx', 'qwen3', 'qwen3-0.6b', 'crisperwhisper', 'whisper') else None
        self.audio_buffer = AudioBuffer(self.config, max_seconds=buffer_seconds)
        self.asr_audio_queue = Queue()
        self.asr_transcript_queue = Queue()
        # Per-run ASR choice from the trigger UI (falls back to config.ini).
        # Whisper consumes the same PCM queue and runs fully offline; import
        # lazily so the Google path never needs faster-whisper installed.
        # Constrain diarization to the pod's known enrolled participants
        # (cap speaker count + remap clusters to enrolled voices), when enabled
        # and the enrolled set is known. Off -> None args -> unchanged behaviour.
        _enrolled = getattr(self, 'speakers', None) or None
        if cf.diarization_constrain_to_enrolled() and _enrolled:
            _max_spk, _enr, _spk_model = len(_enrolled), _enrolled, diarization_model
        else:
            _max_spk, _enr, _spk_model = None, None, None
        if getattr(self, 'asr_choice', None) in ('qwen3', 'qwen3-0.6b'):
            from asr_connectors.qwen3_asr import Qwen3ASR
            qwen_model = 'Qwen/Qwen3-ASR-0.6B' if self.asr_choice == 'qwen3-0.6b' else 'Qwen/Qwen3-ASR-1.7B'
            self.asr = Qwen3ASR(self.asr_audio_queue, self.asr_transcript_queue, self.config, self.stream_data, self.interval,
                                audio_file=self.audio_file, model_id=qwen_model,
                                diarize=(getattr(self, 'diarizer_choice', None) == 'pyannote'),
                                max_speakers=_max_spk, enrolled=_enr, speaker_model=_spk_model)
        elif getattr(self, 'asr_choice', None) == 'whisperx':
            from asr_connectors.whisperx_asr import WhisperXASR
            self.asr = WhisperXASR(self.asr_audio_queue, self.asr_transcript_queue, self.config, self.stream_data, self.interval,
                                   audio_file=self.audio_file, model_size=cf.whisper_model_size(),
                                   diarize=(getattr(self, 'diarizer_choice', None) == 'pyannote'),
                                   diarizer=(getattr(self, 'diarizer_choice', None)
                                             if getattr(self, 'diarizer_choice', None) in ('pyannote', 'sortformer') else None),
                                   max_speakers=_max_spk, enrolled=_enr, speaker_model=_spk_model)
        elif getattr(self, 'asr_choice', None) in ('crisperwhisper', 'whisper'):
            # 'whisper' is a legacy alias: plain Whisper was retired in
            # favour of CrisperWhisper.
            from asr_connectors.crisperwhisper_asr import CrisperWhisperPosthocASR
            self.asr = CrisperWhisperPosthocASR(self.asr_audio_queue, self.asr_transcript_queue, self.config, self.stream_data, self.interval,
                                                audio_file=self.audio_file, model_id=cf.crisperwhisper_model(),
                                                mode=cf.crisperwhisper_mode())
        else:
            self.asr = GoogleASR(self.asr_audio_queue, self.asr_transcript_queue, self.config, self.stream_data,self.interval,STOP_SIGNAL)
        self.asr.start()
        self.processor = AudioProcessorPosthoc(self.audio_buffer, self.asr_transcript_queue, diarization_model, get_semantic_model(getattr(self, 'embedder_choice', None)), self.config, scorer=getattr(self, 'scorer_choice', None))
        # Full re-run REPLACES the pod's previous transcript-level results.
        callbacks.post_posthoc_reset(self.config.auth_key, 'audio')
        self.processor.start()
        self._run_active = True  # run may now finish in the background if the client leaves
        # Store the processor (not just a flag) so a reconnecting client can
        # re-attach to it and resume receiving progress.
        running_audio_processes[self.config.auth_key] = self.processor
        batch_asr = getattr(self, 'asr_choice', None) in ('whisperx', 'qwen3', 'qwen3-0.6b', 'crisperwhisper', 'whisper')
        self.audioreader = AudioStreamReader(self.audio_buffer, self.asr_audio_queue, self.audio_file, self.queue_put_timeout,self.config,STOP_SIGNAL,running_audio_processes, realtime=not batch_asr)
        

    def _recover_audio_from_video(self):
        # The capture sometimes writes a header-only wav while the webm carries
        # a perfectly good audio track (e.g. pod 648). Extract it over the stub
        # in place so the run proceeds with real audio.
        try:
            import glob as _glob, subprocess as _sp
            vid_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   'video_processing', 'videorecordings')
            vids = sorted(_glob.glob(os.path.join(vid_dir, '{0}-*_orig.webm'.format(self.session_device_id)))
                          + _glob.glob(os.path.join(vid_dir, '{0}-*_orig.mp4'.format(self.session_device_id))),
                          key=os.path.getsize, reverse=True)
            if not vids:
                return False
            _sp.run(['ffmpeg', '-y', '-v', 'error', '-i', vids[0], '-vn',
                     '-ar', '16000', '-ac', '1', '-acodec', 'pcm_f32le',
                     str(self.audio_file)], check=True, timeout=600)
            ok = os.path.getsize(str(self.audio_file)) >= 10000
            logging.warning('Recovered audio for %s from video (%d bytes): %s',
                            self.session_device_id, os.path.getsize(str(self.audio_file)), ok)
            return ok
        except Exception as e:
            logging.warning('Audio recovery from video failed for %s: %s', self.session_device_id, e)
            return False

    def signal_end(self):
        # If a full run is still processing, a disconnect / heartbeat timeout
        # must NOT kill it — let it finish in the background and mark itself
        # complete (on_run_complete). Only tear down when nothing is running.
        if getattr(self, '_run_active', False):
            # The socket-local flag can outlive the run: a status probe that
            # adopted the run and dropped before completion cleanup reached
            # it keeps _run_active forever, and the inactivity sweep then
            # logs "run active" every 10s for a finished run. Trust the run
            # registry instead; if this pod has no live entry, clear the
            # flag and fall through to normal teardown.
            key = getattr(getattr(self, 'config', None), 'auth_key', None)
            if key and key in running_audio_processes:
                logging.info("Client gone but audio post-hoc run active — continuing in background.")
                return
            self._run_active = False
        if self.asr:
            self.asr.stop()
        if self.processor:
            self.processor.stop()
        if self.audioreader:
            self.audioreader.stop()
        if self.processorspeakermetric:
            self.processorspeakermetric.stop()

        if self.config:
            cm.remove(self, self.config.session_key, self.config.auth_key)

    def on_run_complete(self):
        # Called by the processor when a full run finishes. Clears the run guard,
        # marks the pod complete server-side (survives a disconnected client),
        # and detaches from the connection manager.
        self._run_active = False
        try:
            if self.config:
                running_audio_processes.pop(self.config.auth_key, None)
                callbacks.post_posthoc_completed(self.config.auth_key, getattr(self, 'model_choices', None))
                cm.remove(self, self.config.session_key, self.config.auth_key)
        except Exception as e:
            logging.warning("on_run_complete cleanup failed: %s", e)
        # Queue-driven pods disconnect right after triggering, so signal_end
        # bails out early (_run_active) and its stop()s never run — worker
        # threads then outlive the run and pin this protocol together with the
        # pod's ENTIRE in-memory audio buffer (~1-2 GiB/pod). That was the
        # host-RAM growth that got the service kernel-OOM-killed every ~8-10
        # batch pods. Stop everything and drop the big references explicitly.
        try:
            for worker in (self.asr, self.processor, self.audioreader,
                           self.processorspeakermetric):
                if worker:
                    try:
                        worker.stop()
                    except Exception:
                        pass
            self.asr = None
            self.processor = None
            self.audioreader = None
            self.processorspeakermetric = None
            self.audio_buffer = None
            import gc
            gc.collect()
            # gc alone leaves the run's freed CUDA blocks reserved by torch's
            # caching allocator, so idle VRAM never returns to baseline between
            # runs; hand the unreferenced blocks back to the GPU.
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
        except Exception as e:
            logging.warning("on_run_complete worker teardown failed: %s", e)


if __name__ == '__main__':
    cf.initialize()
    # Initialize Logger
    dir_path = os.path.dirname(os.path.realpath(__file__))
    log_format = logging.Formatter('%(asctime)s - %(levelname)s: %(message)s')
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    log_file = logging.FileHandler(os.path.join(dir_path, 'audio_posthoc_processing_service.log'))
    log_file.setFormatter(log_format)
    logger.addHandler(log_file)
    log_console = logging.StreamHandler()
    log_console.setFormatter(log_format)
    logger.addHandler(log_console)


    # Run Server
    logging.info('Starting Audio Posthoc Processing Service...')
    features_detector.initialize()
    keyword_detector.initialize(cf.keyword_model_limit())

    poll_connections = task.LoopingCall(cm.check_connections)
    poll_connections.start(10.0)
    auth_connections = task.LoopingCall(cm.check_connection_authentication)
    auth_connections.start(5.0)
    factory = WebSocketServerFactory()
    factory.protocol = ServerProtocol
    reactor.listenTCP(int(os.environ.get("DC_AUDIO_POSTHOC_WS_PORT", 9005)), factory)
    logging.info('Audio Posthoc Processing Service started.')
    callbacks.post_service_restarted('audio')
    reactor.run()
    logging.info('Audio Posthoc Processing Service ended.')