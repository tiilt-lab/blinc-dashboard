import os as _rs_os, sys as _rs_sys  # noqa: E401
_rs_c = _rs_os.path.dirname(_rs_os.path.abspath(__file__))
while _rs_c != '/' and not _rs_os.path.isdir(_rs_os.path.join(_rs_c, 'common')):
    _rs_c = _rs_os.path.dirname(_rs_c)
_rs_c = _rs_os.path.join(_rs_c, 'common')
if _rs_c not in _rs_sys.path:
    _rs_sys.path.insert(0, _rs_c)
import reactor_safety  # reactor/thread boundary; src/common bootstrapped above
from ws_protocol import WsMessageMixin  # shared onMessage/onClose
import os
import time
import logging
import callbacks
import threading
import traceback
import config as cf
import numpy as np
import torch
# torch>=2.6 defaults weights_only=True, which rejects several of the local
# model checkpoints (yolov4/crowdhuman pickles, gaze weights with numpy
# globals). These files ship with the deployment and are trusted.
_torch_load_original = torch.load
def _torch_load_permissive(*args, **kwargs):
    kwargs.setdefault('weights_only', False)
    return _torch_load_original(*args, **kwargs)
torch.load = _torch_load_permissive
from pathlib import Path
from recorder import VidRecorder
from processing_config import ProcessingConfig
from connection_manager import ConnectionManager  # also puts src/common on sys.path
import safe_names
from video_cartoonizer.videoprocessor_posthoc import VideoProcessorPosthoc
from twisted.internet import reactor, task
from autobahn.twisted.websocket import WebSocketServerFactory
from autobahn.twisted.websocket import WebSocketServerProtocol
from facial_biometric_processing_service import FacialBiometricProcessor
from emotion_detector.emotion_detection_model import EmotionDetectionModelV1
from attention_tracking.detect import ImageObjectDetection
from attention_tracking.attention_tracking import AttentionDetection
from video_cartoonizer.VideoMetricProcessor import VideoMetricAnalytics

STOP_SIGNAL = object()
cm = ConnectionManager()
# Gaze backend is config-selected: Gaze-LLE (open SOTA) or GazeFollow.
if cf.attention_model() == 'gazelle':
    from attention_tracking.gazelle_attention import GazeLLEAttentionDetection
    attention_detection = GazeLLEAttentionDetection()
else:
    attention_detection = AttentionDetection()
# Emotion backend is config-selected: 'hsemotion' (AffectNet-8, open SOTA)
# or the original ResMaskingNet.
if cf.emotion_model() == 'hsemotion':
    from emotion_detector.hsemotion_model import EmotionDetectionModelV2
    facial_emotion_detector = EmotionDetectionModelV2()
else:
    facial_emotion_detector = EmotionDetectionModelV1()
image_object_detection = ImageObjectDetection(STOP_SIGNAL,source="post_hoc")
video_metric_analytics = VideoMetricAnalytics(attention_detection, facial_emotion_detector, image_object_detection,STOP_SIGNAL,source="post_hoc")
batch_size = 40
# Cross-process registry of pods under posthoc video processing (see
# common/pod_registry). Redis claim + local object dict, drop-in for the old
# dict, TTL self-heal. prefix distinct from audio so the two don't collide.
import redis_client
from pod_registry import PodRegistry
running_video_processes = PodRegistry(redis_factory=redis_client._redis,
                                      prefix='video-posthoc:')
# Any message that names a pod must carry a ticket the API minted for THAT pod
# (common/posthoc_ticket): this socket is reachable by anyone through nginx,
# and a start wipes the pod's previous analysis. The enrollment message
# (save-audio-video-fingerprinting) names no pod; it carries instead the
# per-alias token /addstudent minted (common/enrollment_token).
from posthoc_ticket import ticket_allows
from enrollment_token import enrollment_allows
_TICKETED_TYPES = frozenset((
    'Initialize_video_processing_analytics',
    'cancel_posthoc',
    'query_posthoc_status',
))
_running_guard = threading.Lock()  # kept for snapshot-then-mutate sections

# Exit once the analysis queue drains (and systemd restarts us fresh):
# glibc never returns the per-run allocation churn to the OS, so RSS only
# resets with a new process. Armed from on_run_complete when the last run
# pops off running_video_processes; a new run starting simply makes the
# timer a no-op (its own completion re-arms it). Dashboard status probes
# connect every couple of minutes, so a busy socket at fire time just
# defers the check rather than abandoning it.
IDLE_RECYCLE_SECONDS = 600
_idle_recycle = None

def schedule_idle_recycle(delay=IDLE_RECYCLE_SECONDS):
    global _idle_recycle
    if _idle_recycle is not None and _idle_recycle.active():
        _idle_recycle.cancel()
    _idle_recycle = reactor.callLater(delay, _idle_recycle_fire)

def _idle_recycle_fire():
    global _idle_recycle
    _idle_recycle = None
    if running_video_processes:
        return
    if cm.get_number_of_connections() > 0:
        schedule_idle_recycle(60)
        return
    logging.info('Analysis queue empty for {0}s - exiting to reclaim run memory (systemd restarts the service).'.format(IDLE_RECYCLE_SECONDS))
    reactor.stop()

# Lazy per-run model caches so a post-hoc run can request a different emotion /
# attention backend than the deployment default without reloading each run
# (mirrors the audio server's get_semantic_model). Seeded with the
# config-selected singletons already loaded above. The object/face/head
# detector stays config-level: it is the heavy, CUDA-lock-serialized model
# shared across concurrent runs.
_EMOTION_CACHE = {cf.emotion_model(): facial_emotion_detector}
_ATTENTION_CACHE = {cf.attention_model(): attention_detection}
# Serialize cache-miss loads so two concurrent runs requesting the same
# not-yet-loaded backend don't both construct the heavy CUDA model (OOM risk).
_MODEL_CACHE_LOCK = threading.Lock()

def get_emotion_detector(name=None):
    name = (name or cf.emotion_model()).strip()
    if name not in _EMOTION_CACHE:
        with _MODEL_CACHE_LOCK:
            if name not in _EMOTION_CACHE:  # re-check under lock
                logging.info("Loading emotion backend '%s' for this run", name)
                if name == 'hsemotion':
                    from emotion_detector.hsemotion_model import EmotionDetectionModelV2
                    _EMOTION_CACHE[name] = EmotionDetectionModelV2()
                else:
                    _EMOTION_CACHE[name] = EmotionDetectionModelV1()
    return _EMOTION_CACHE[name]

def get_attention_detector(name=None):
    name = (name or cf.attention_model()).strip()
    if name not in _ATTENTION_CACHE:
        with _MODEL_CACHE_LOCK:
            if name not in _ATTENTION_CACHE:  # re-check under lock
                logging.info("Loading attention backend '%s' for this run", name)
                if name == 'gazelle':
                    from attention_tracking.gazelle_attention import GazeLLEAttentionDetection
                    _ATTENTION_CACHE[name] = GazeLLEAttentionDetection()
                elif name == 'page-vith+':
                    from attention_tracking.page_attention import PageAttentionDetection
                    _ATTENTION_CACHE[name] = PageAttentionDetection()
                else:
                    _ATTENTION_CACHE[name] = AttentionDetection()
                # Cache-miss backends are constructed lazily, so load weights
                # now: a bad selection (e.g. PAGE without its checkpoint)
                # fails here — surfaced to the client — instead of mid-run.
                try:
                    _ATTENTION_CACHE[name].init_model(batch_size)
                except Exception:
                    del _ATTENTION_CACHE[name]
                    raise
    return _ATTENTION_CACHE[name]

class ServerProtocol(WsMessageMixin, WebSocketServerProtocol):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_message = time.time()
        self.stream_data = False
        self.facial_emotion_detector = facial_emotion_detector
        self.image_object_detection = image_object_detection
        self.attention_detection = attention_detection
        self.video_metric_analytics = video_metric_analytics
        self.interval = 10
        self.batch_size = batch_size
        # Consulted by the connection manager's periodic key check and by
        # signal_end cleanup; post-hoc connections aren't live-key gated.
        self.running = False
        self.config = None
        # Never-initialized connections (status probes) must not crash cleanup.
        self.video_processor = None
        # True while a run is processing; keeps signal_end from killing it when
        # the client disconnects (the run finishes + marks itself complete).
        self._run_active = False

        cm.add(self)
        logging.info('New client connected...')

    # onMessage / onClose come from WsMessageMixin (shared across both services).

    def _authorised(self, data):
        if ticket_allows(redis_client._redis(), data.get('ticket'), data.get('sessiondeviceid')):
            return True
        self.send_json({'type': 'error', 'message': 'Not authorised for this pod.'})
        return False

    def process_json(self, data):
        if not 'type' in data:
            logging.warning('Message does not contain "type".')
            return
        if data['type'] in _TICKETED_TYPES and not self._authorised(data):
            return
        
        if data['type'] == 'save-audio-video-fingerprinting':
            # Gate first (same as the audio side): a passing enrollment
            # replaces the alias's face embedding, and process_binary only
            # accepts media once stream_data is set below.
            if not enrollment_allows(redis_client._redis(), data.get('token'), data.get('alias')):
                logging.warning('rejected fingerprint enrollment: bad token for alias %r', data.get('alias'))
                self.send_json({'type': 'error', 'message': 'Enrollment not authorised. Start again from the sign-up page.'})
                return
            self.currStudent = data['id']
            self.stream_data = data['streamdata']
            # alias/extension build file paths — sanitize or reject.
            try:
                self.currAlias = safe_names.safe_name(data['alias'])
            except safe_names.UnsafeName:
                logging.warning('rejected fingerprint enrollment: unsafe alias %r', data.get('alias'))
                self.send_json({'type': 'error', 'message': 'Invalid name.'})
                return
            self.mediaExt = safe_names.safe_media_ext(data['mimeextension'])
            if self.stream_data == 'audio-video-fingerprint':
                    self.video_file = os.path.join(cf.video_recordings_folder(), "{0}".format(self.currAlias))
                    self.vid_recorder = VidRecorder(self.video_file,"none","none",cf.video_record_original(),16000, 2, 1,self.mediaExt)
                    facial_biometric_file = os.path.join(cf.facial_embedding_folder(), "{0}".format(self.currAlias))
                    self.facial_processor = FacialBiometricProcessor(facial_biometric_file,self.video_file,self.mediaExt,self.currAlias)
                    self.facial_processor.add_websocket_connection(self)
                    
            logging.info('save-audio-video-fingerprinting: video process connected')
            self.send_json({'type':'saveaudiovideo'})

        if data['type'] == 'Initialize_video_processing_analytics':
            self.sessionid = data['sessionid']
            self.session_device_id = data['sessiondeviceid']
            self.server_start= data['server_start']
            self.facial_embeddings = dict()
            self.sample_rate = 16000
            self.video_file = self.get_video_file_path(self.session_device_id)

            if not self.video_file:
                self.send_json({'type': 'error', 'message': 'No video captured for this group.'})
            else:
                self.video_file = str(self.video_file)
                file_path_split = self.video_file.split("(")
                key = file_path_split[0].split("/")[-1].split("_")[0]
                off_set_date = file_path_split[1].split(")")[0]
                logging.info("key is {} and offset date is {}".format(key,off_set_date))

                #keep track of currently running posthoc video analytics
                # Atomic cross-process claim; registry.pop releases it.
                if not running_video_processes.try_claim(key):
                    self.send_json({'type': 'error', 'message': 'Video posthoc analytics for this group is already running'})
                    return

                conf_val = {'key':key,'encoding': "pcm_f16le", 'sample_rate': self.sample_rate,'channels': 2,'sessionid': self.sessionid,'deviceid': self.session_device_id,
                            'server_start':self.server_start,'off_set_date':off_set_date}
                valid, result = ProcessingConfig.from_json(conf_val,source="posthoc processing")
                if not valid:
                    # Release the claim or this pod is blocked until restart.
                    running_video_processes.pop(key, None)
                    logging.info("Configuration setting failed for video posthoc processing")
                else:
                    self.config = result
                    # Optional per-run model choices from the trigger UI; fall
                    # back to the deployment config when the field is absent.
                    try:
                        self.facial_emotion_detector = get_emotion_detector(data.get('emotion_model'))
                        self.attention_detection = get_attention_detector(data.get('attention_model'))
                    except Exception as e:
                        # e.g. PAGE selected but its checkpoint isn't
                        # installed — fail the run with the actionable message
                        # instead of dying mid-analysis.
                        logging.exception("Per-run model selection failed")
                        running_video_processes.pop(key, None)
                        self.send_json({'type': 'error', 'message': str(e)})
                        return
                    self.video_metric_analytics = VideoMetricAnalytics(
                        self.attention_detection, self.facial_emotion_detector,
                        self.image_object_detection, STOP_SIGNAL, source="post_hoc")
                    # Fresh head-track state for this run — a rerun must not
                    # inherit locked identities from the previous pass.
                    self.image_object_detection.reset_session_tracker(self.config.auth_key)
                    # Optional TalkNCE active-speaker-detection pass, per-run.
                    self.asd_choice = data.get('asd') or None
                    self.asd_candidates = [s.get('alias') for s in (data.get('speakers') or [])
                                           if s.get('alias')]
                    self.model_choices = {
                        'emotion_model': data.get('emotion_model') or cf.emotion_model(),
                        'attention_model': data.get('attention_model') or cf.attention_model(),
                        'object_model': cf.object_model(),
                        'face_model': cf.face_model(),
                        'head_model': cf.head_model(),
                    }
                    if self.asd_choice:
                        self.model_choices['asd_model'] = 'talknce-unitalk'
                    self.frame_dir = os.path.join(cf.video_recordings_folder(), "vid_img_frames_{0}_{1}_{2}_({3})".format(self.config.auth_key,self.config.sessionId,self.config.deviceId,  str(time.ctime())))
                    #start processing
                    for speaker in data['speakers']:
                        try:
                            alias = safe_names.safe_name(speaker["alias"])
                        except safe_names.UnsafeName:
                            logging.warning('skipping speaker with unsafe alias %r', speaker.get("alias"))
                            continue
                        facial_embedding_file = os.path.join(cf.facial_embedding_folder(), "{0}".format(alias))
                        try:
                            # Embeddings are written by our own enrollment pipeline;
                            # allow_pickle is required to read the dict payload. The
                            # path is now sanitized so it can't point off-tree.
                            facials = np.load(facial_embedding_file+".npy", allow_pickle=True).item()
                            self.facial_embeddings[speaker["id"]] = {"alias": speaker["alias"], "data": facials[speaker["alias"]]}
                        except Exception:
                            error_str = traceback.format_exc()
                            logging.info("error loading facial embedding for {} : {}".format(speaker["alias"],error_str))

                    self.signal_start()
                    self.video_processor.setParticpantFacialEmbeddings(self.facial_embeddings)

                    self.send_json({'type':'init posthoc analytics completed','message':"Starting Video Analytics Processing"})
                    logging.info('Video Posthoc analytics initiated')

        if data['type'] == 'start_posthoc_video_processing':
            self.send_json({'type':'video posthoc analytics started','message':"Processing Video posthoc Analytics"})
            # Full re-run REPLACES the pod's previous video metrics.
            callbacks.post_posthoc_reset(self.config.auth_key, 'video')
            self.video_processor.add_websocket_connection(self)
            self.video_processor.start()
            self._run_active = True  # may now finish in the background if the client leaves
            # Store the processor so a reconnecting client can re-attach.
            running_video_processes[self.config.auth_key] = self.video_processor

        if data['type'] == 'cancel_posthoc':
            sdid = str(data.get('sessiondeviceid', ''))
            hits = [k for k in list(running_video_processes.keys()) if k.startswith(sdid + '-')]
            for k in hits:
                proc = running_video_processes.pop(k, None)
                if hasattr(proc, 'stop'):
                    try:
                        proc.stop()
                    except Exception:
                        pass
            self.send_json({'type': 'posthoc_cancelled', 'cancelled': len(hits)})

        if data['type'] == 'query_posthoc_status':
            sdid = str(data.get('sessiondeviceid', ''))
            match = next((k for k in list(running_video_processes.keys())
                          if k.startswith(sdid + '-')), None)
            self.send_json({'type': 'posthoc_status', 'running': match is not None})
            if match is not None:
                # Re-attach the running processor to THIS socket (live progress
                # resumes, incl. an immediate replay of the last percent).
                proc = running_video_processes.get(match)
                if hasattr(proc, 'add_websocket_connection'):
                    proc.add_websocket_connection(self)
                    self.config = getattr(proc, 'config', None) or self.config
                    self._run_active = True

        if data['type'] == 'heartbeat':
            auth_key = data.get('key', None)
            logging.info("Recieved Heartbeat from client with authkey {0}".format(auth_key))
        if data['type'] == 'heartbeat_from_posthoc_processing':
            auth_key = data.get('key', None)
            logging.info("Recieved Heartbeat from client with authkey {0}".format(auth_key))   


    def process_binary(self, data):
        if self.stream_data == 'audio-video-fingerprint':
            # Each blob is a complete standalone take (same contract as the
            # audio side): start it on a fresh scratch file — VidRecorder
            # appends, so a retake used to land AFTER take 1 in the same file
            # and the embedding run re-read the old take.
            try:
                os.remove(self.video_file + "." + self.mediaExt)
            except OSError:
                pass
            self.vid_recorder.write(data,self.video_file+"."+self.mediaExt)

            self.facial_processor.start()
        else:
            self.send_json({'type': 'error', 'message': 'Binary audio data sent before start message.'})

    def get_video_file_path(self,sessionDeviceId):
        current_dir = Path(__file__).resolve().parent
        # Go up one level (parent)
        parent_dir = current_dir.parent
        # Go into sibling directory
        target_dir = parent_dir / "video_processing" / "videorecordings"
        # Match the web server's discovery (routes/session.py
        # _find_original_videos): accept .webm AND .mp4 — uploaded sessions
        # store *_orig.mp4, and the old *.webm-only glob made those pods
        # report "No video captured" with the file sitting right there. The
        # "-" after the id keeps 91 from matching 918-*. Newest by mtime.
        # Coerce to int: the id arrives untyped off the socket and is used as a
        # glob prefix, so int() blocks path-traversal patterns.
        try:
            prefix = str(int(sessionDeviceId))
        except (TypeError, ValueError):
            logging.warning("get_video_file_path: non-integer device id %r", sessionDeviceId)
            return None
        for pattern in (f"{prefix}-*_orig.webm", f"{prefix}-*.webm",
                        f"{prefix}-*_orig.mp4", f"{prefix}-*.mp4"):
            files = sorted(target_dir.glob(pattern),
                           key=lambda p: p.stat().st_mtime)
            if files:
                file_path = files[-1]
                logging.info("Found: {}".format(file_path))
                return file_path
        logging.info("No video file found for session device %s", prefix)
        return None
    


    def send_json(self, message):
        # Reactor-safe transport write (was a raw sendMessage). See
        # common/reactor_safety.
        reactor_safety.send_json(self, message)

    def signal_start(self):
        self.video_processor = VideoProcessorPosthoc(self.facial_emotion_detector,self.image_object_detection,self.attention_detection,self.video_metric_analytics,
                                                     self.video_file,self.config,running_video_processes,self.interval,self.batch_size,self.frame_dir,STOP_SIGNAL,save_gaze_annotation=True)
    def signal_end(self):
        # Don't kill an in-flight run on a client disconnect / timeout — it
        # finishes in the background and marks itself complete (on_run_complete).
        if getattr(self, '_run_active', False):
            logging.info("Client gone but video post-hoc run active — continuing in background.")
            return
        if self.video_processor:
            self.video_processor.stop()

    def on_run_complete(self):
        # Called by the processor when the run finishes: release the guard and
        # mark the pod complete server-side (survives a disconnected client).
        self._run_active = False
        # Per-pod CUDA cleanup: batch runs grew this process ~1.5 GiB per pod
        # until whisperx in the AUDIO service could no longer load (shared
        # card) - same class of leak fixed in whisperx_asr. Runs BEFORE the
        # optional ASD subprocess so that pass gets the freed headroom.
        try:
            import gc
            self.video_processor = None
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                logging.info("video posthoc: released CUDA cache (%.1f GiB still reserved)",
                             torch.cuda.memory_reserved() / 2**30)
        except Exception as e:
            logging.warning("video posthoc CUDA cleanup failed: %s", e)
        # Optional active-speaker-detection pass (TalkNCE-UniTalk). Synchronous
        # so the artifact exists before the pod is stamped complete; best-effort
        # so an ASD failure never fails the video run itself.
        try:
            if getattr(self, 'asd_choice', None) == 'talknce' and getattr(self, 'video_file', None):
                self.run_asd_pass()
        except Exception as e:
            logging.warning("ASD pass failed: %s", e)
        try:
            if self.config:
                running_video_processes.pop(self.config.auth_key, None)
                callbacks.post_posthoc_completed(self.config.auth_key, getattr(self, 'model_choices', None))
        except Exception as e:
            logging.warning("video on_run_complete cleanup failed: %s", e)
        # Called from the processor's worker thread; callLater must happen on
        # the reactor thread.
        if not running_video_processes:
            reactor.callFromThread(schedule_idle_recycle)

    def run_asd_pass(self):
        import subprocess
        import sys as _sys
        video_path = str(self.video_file)
        artifact = os.path.splitext(video_path)[0] + '_asd.json'
        runner = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'asd', 'run_asd.py')
        candidates = ','.join(getattr(self, 'asd_candidates', []) or [])
        cmd = [_sys.executable, runner, video_path, '--out', artifact,
               '--gallery', cf.facial_embedding_folder()]
        if candidates:
            cmd += ['--candidates', candidates]
        logging.info("ASD: launching TalkNCE pass for %s", video_path)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90 * 60)
        if proc.returncode != 0:
            raise RuntimeError("run_asd failed (rc={0}): {1}".format(
                proc.returncode, (proc.stderr or '')[-2000:]))
        logging.info("ASD: artifact written: %s", artifact)


if __name__ == '__main__':
    cf.initialize()
    # Initialize Logger
    dir_path = os.path.dirname(os.path.realpath(__file__))
    log_format = logging.Formatter('%(asctime)s - %(levelname)s: %(message)s')
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    log_file = logging.FileHandler(os.path.join(dir_path, 'video_posthoc_processing_service.log'))
    log_file.setFormatter(log_format)
    logger.addHandler(log_file)
    log_console = logging.StreamHandler()
    log_console.setFormatter(log_format)
    logger.addHandler(log_console)

    # Init and load share model resources
    image_object_detection.init_model(batch_size) 
    attention_detection.init_model(batch_size) 
    facial_emotion_detector.load_model() 
    # image_object_detection.start()
    # video_metric_analytics.start() 

    # Run Server
    logging.info('Starting Video Posthoc Processing Service...')
    callbacks.post_service_restarted('video')
    poll_connections = task.LoopingCall(cm.check_connections)
    poll_connections.start(10.0)
    auth_connections = task.LoopingCall(cm.check_connection_authentication)
    auth_connections.start(5.0)
    factory = WebSocketServerFactory()
    factory.protocol = ServerProtocol
    # Loopback only: nginx and the API's queue connect via 127.0.0.1.
    reactor.listenTCP(int(os.environ.get("DC_VIDEO_POSTHOC_WS_PORT", 9004)), factory, interface='127.0.0.1')
    logging.info('Video Posthoc Processing Service started.')
    reactor.run()
    logging.info('Video Posthoc Processing Service ended.')