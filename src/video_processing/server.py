import os
import time
import queue
import logging
import threading
import subprocess
import callbacks
import config as cf
import numpy as np
try:
    import moviepy.editor as mp  # moviepy 1.x  # noqa: F401
except ImportError:
    pass  # moviepy 2.x dropped the .editor module

from recorder import VidRecorder
from processing_config import ProcessingConfig
from connection_manager import ConnectionManager  # also puts src/common on sys.path
import os as _rs_os, sys as _rs_sys  # noqa: E401
_rs_c = _rs_os.path.dirname(_rs_os.path.abspath(__file__))
while _rs_c != '/' and not _rs_os.path.isdir(_rs_os.path.join(_rs_c, 'common')):
    _rs_c = _rs_os.path.dirname(_rs_c)
_rs_c = _rs_os.path.join(_rs_c, 'common')
if _rs_c not in _rs_sys.path:
    _rs_sys.path.insert(0, _rs_c)
import reactor_safety  # reactor/thread boundary; src/common bootstrapped above
from ws_protocol import WsMessageMixin  # shared onMessage/onClose
import audio_bytes
import safe_names
# Cartoonify is optional (and disabled in this instance's config); its
# vendored tree still uses the moviepy 1.x import path, so don't let it
# take down the whole live video service.
try:
    from video_cartoonizer.videoprocessor import VideoProcessor
except Exception as _vc_err:
    VideoProcessor = None
    print("video_cartoonizer unavailable ({}) - cartoonify disabled".format(_vc_err))
from datetime import datetime
from twisted.internet import reactor, task
from autobahn.twisted.websocket import WebSocketServerFactory
from autobahn.twisted.websocket import WebSocketServerProtocol
from queue import Full, Empty
from video_cartoonizer.video_cartoonify_loader import VideoCartoonifyLoader
from emotion_detector.emotion_detection_model import EmotionDetectionModel, EmotionDetectionModelV1
from attention_tracking.detect import ImageObjectDetection
from attention_tracking.attention_tracking import AttentionDetection
from video_cartoonizer.VideoMetricProcessor import VideoMetricAnalytics

STOP_SIGNAL = object()
cm = ConnectionManager()

# Largest legitimate websocket message: one MediaRecorder chunk (the client
# sends each blob as its own message) of CHUNK_SECONDS at the client's bitrate
# ceiling — 5 Mbps video + 128 kbps audio (byod-join-component.jsx). The
# factory caps payloads at 2x that; anything larger is not a video chunk.
CHUNK_SECONDS = 10
MAX_VIDEO_CHUNK_BYTES = (5_000_000 + 128_000) * CHUNK_SECONDS // 8   # ~6.4 MB
MAX_WS_MESSAGE_BYTES = 2 * MAX_VIDEO_CHUNK_BYTES                     # ~12.8 MB

# Aggregate throughput gauge. Sums video bytes actually delivered across all
# connections (a monotonic module-level counter, so disconnects can't skew
# the delta) and logs the rate every GAUGE_SECONDS. While any pod is
# lag-warned the link is running flat-out, so the logged rate approximates
# the venue's usable uplink capacity — measured passively, per venue, with
# no one in the room doing anything. (Tonight's incident: six pods offered
# ~17 Mbps into a room that delivered ~5.)
GAUGE_SECONDS = 30
_rx_total = {'bytes': 0}
_gauge_last = {'bytes': 0}
_lagging = set()

def _throughput_gauge():
    delta = _rx_total['bytes'] - _gauge_last['bytes']
    _gauge_last['bytes'] = _rx_total['bytes']
    n = cm.get_number_of_connections()
    if n == 0 or delta <= 0:
        return
    mbps = (delta * 8.0) / GAUGE_SECONDS / 1e6
    logging.info(
        'AGGREGATE_VIDEO_THROUGHPUT mbps=%.2f pods=%d lagging=%d%s',
        mbps, n, len(_lagging),
        ' — link saturated; this approximates venue uplink capacity'
        if _lagging else '')

# Recycle the process once the last client has been gone for a while.
# Analysis memory cannot be returned to the OS (glibc arena fragmentation
# under the pipeline's numpy frame churn), so RSS is a high-water mark that
# only a fresh process resets — ~18 GB after one multi-pod afternoon.
# systemd (Restart=always) brings the service straight back; the delay
# keeps mid-session refreshes and reconnects safe, since a restart means
# ~40 s of model loading with the port closed.
IDLE_RECYCLE_SECONDS = 600
_idle_recycle = None

def cancel_idle_recycle():
    global _idle_recycle
    if _idle_recycle is not None and _idle_recycle.active():
        _idle_recycle.cancel()
    _idle_recycle = None

def schedule_idle_recycle():
    global _idle_recycle
    cancel_idle_recycle()
    _idle_recycle = reactor.callLater(IDLE_RECYCLE_SECONDS, _idle_recycle_fire)

def _idle_recycle_fire():
    if cm.get_number_of_connections() == 0:
        logging.info('No clients for {0}s - exiting to reclaim analysis memory (systemd restarts the service).'.format(IDLE_RECYCLE_SECONDS))
        reactor.stop()

class StreamingChunkDecoder:
    """Incremental analytics decoder.

    The old path re-opened the pod's ENTIRE accumulated recording with
    moviepy for every 10-second chunk, on the reactor thread. Cost grew
    linearly with the file (~0.25s/MB — 28s per chunk at 100MB), the
    reactor starved, socket reads stopped, and ingest collapsed to
    1.5-6 Mbps total while pods queued minutes of video in their tabs.

    This decoder holds ONE long-lived ffmpeg per connection and feeds it
    each websocket blob as it arrives, so every byte is decoded exactly
    once — O(chunk) per chunk, O(recording) total — and nothing runs on
    the reactor thread. Frames stream to the consumer through a bounded
    queue-backed generator: if the consumer lags, frames are dropped
    (analytics degrades to sampling); nothing ever blocks the feed path.

    A pod's recorder can restart mid-connection (client watchdog): the
    new stream opens with a fresh container header (EBML magic for webm,
    ftyp for mp4), which we detect and answer with a fresh ffmpeg.
    """

    FPS = 10
    FEED_QUEUE_BLOBS = 64
    # Decoder thread cap (audit B.3): libvpx/dav1d otherwise start a thread
    # per core for EVERY pod; two keep up with a 10 fps analytics feed.
    DECODE_THREADS = 2
    # Optional height cap for the analytics feed (0 = native). Left OFF on
    # purpose: the head/object detectors letterbox to 640 px and would not
    # care, but face recognition crops each face from the NATIVE frame and
    # gates it at 120 px (detect.prepare_quality_face min_size) before
    # dlib's CNN, and the gaze head crop is native too. Phones capture
    # 1080p (ideal 1920x1080), so a 720p rescale shrinks every face by a
    # third and pushes small/far faces under that gate — a metrics change,
    # not a memory-only one. Frame memory is bounded downstream instead
    # (bounded_batch_queue). Set to 720 once those gates are frame-relative.
    ANALYTICS_MAX_HEIGHT = 0

    @staticmethod
    def _analytics_dims(width, height, max_height):
        # Output frame size for the rawvideo reader: native, or scaled to
        # max_height keeping aspect with an even width (chroma subsampling).
        if not max_height or height <= max_height:
            return width, height
        w = int(round(width * max_height / float(height)))
        return max(2, w - (w % 2)), int(max_height)

    def __init__(self, interval, sink, label):
        self.interval = interval
        self.sink = sink            # callable(iterable of (ts, frame))
        self.label = label
        self.feed_queue = queue.Queue(maxsize=self.FEED_QUEUE_BLOBS)
        self.dead = False
        self.proc = None
        self.width = None
        self.height = None
        self.reader = None
        self.batches_done = 0
        # Last container header seen (EBML/ftyp init segment). MediaRecorder
        # emits it exactly once per recorder start, so it is cached to
        # recover from a mid-stream pipe break without waiting for a client
        # recorder restart.
        self.init_blob = None
        self.pump = threading.Thread(
            target=self._pump, name='chunkdec-' + label[:12], daemon=True)
        self.pump.start()

    @staticmethod
    def _is_header(blob):
        return (blob[:4] == b'\x1a\x45\xdf\xa3' or          # webm/EBML
                (len(blob) > 8 and blob[4:8] == b'ftyp'))   # fragmented mp4

    # ---- reactor-thread side: must never block ----
    def feed(self, blob):
        if self.dead:
            return
        try:
            self.feed_queue.put_nowait(bytes(blob))
        except Full:
            logging.warning(
                'analytics decoder behind for %s - dropping a blob '
                '(recording on disk is unaffected)', self.label)

    def stop(self):
        self.dead = True
        try:
            self.feed_queue.put_nowait(None)
        except Full:
            # Pump is wedged on a full queue; it checks self.dead anyway.
            pass

    # ---- worker threads ----
    def _pump(self):
        try:
            while True:
                blob = self.feed_queue.get()
                if blob is None or self.dead:
                    break
                is_header = self._is_header(blob)
                if is_header:
                    self.init_blob = blob
                if self.proc is None or is_header:
                    # After a pipe break, respawn from the CACHED init
                    # segment: the client only ever sends the header once per
                    # recorder start, so "wait for the next header" meant
                    # dead analytics for the rest of the session, plus one
                    # doomed ffprobe against a mid-stream cluster per chunk.
                    header = blob if is_header else self.init_blob
                    if header is None or not self._respawn(header):
                        continue
                    if not is_header:
                        try:
                            self.proc.stdin.write(header)
                        except Exception:
                            self._kill_proc()
                            continue
                try:
                    self.proc.stdin.write(blob)
                except Exception as e:
                    logging.warning(
                        'analytics decoder pipe broke for %s (%s) - '
                        'will restart from the cached header on the next blob',
                        self.label, e)
                    self._kill_proc()
        finally:
            self._kill_proc()

    def _respawn(self, header_blob):
        self._kill_proc()
        dims = self._probe_dims(header_blob)
        if not dims:
            logging.warning(
                'analytics decoder could not probe dimensions for %s - '
                'skipping until the next header', self.label)
            return False
        self.width, self.height = self._analytics_dims(dims[0], dims[1], self.ANALYTICS_MAX_HEIGHT)
        # Explicit scale dims (not ffmpeg's -2 auto width) so the rawvideo
        # reader's frame size is computed here, not guessed from its rounding.
        vf = 'fps={0}'.format(self.FPS)
        if (self.width, self.height) != tuple(dims):
            vf += ',scale={0}:{1}'.format(self.width, self.height)
        self.proc = subprocess.Popen(
            ['ffmpeg', '-v', 'error', '-threads', str(self.DECODE_THREADS),
             '-i', 'pipe:0', '-vf', vf, '-pix_fmt', 'rgb24',
             '-f', 'rawvideo', 'pipe:1'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        self.reader = threading.Thread(
            target=self._read_frames, args=(self.proc, self.width, self.height),
            name='chunkrd-' + self.label[:12], daemon=True)
        self.reader.start()
        logging.info('analytics decoder started for %s (%dx%d)',
                     self.label, self.width, self.height)
        return True

    def _probe_dims(self, header_blob):
        try:
            out = subprocess.run(
                ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                 '-show_entries', 'stream=width,height', '-of', 'csv=p=0',
                 'pipe:0'],
                input=header_blob, capture_output=True, timeout=10)
            w, h = out.stdout.decode().strip().split(',')[:2]
            w, h = int(w), int(h)
            return (w, h) if w > 0 and h > 0 else None
        except Exception:
            return None

    def _kill_proc(self):
        p, self.proc = self.proc, None
        if p is not None:
            try:
                p.stdin.close()
            except Exception:
                pass
            try:
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                    # Reap after kill or the child sits as a zombie until
                    # CPython's lazy sweep on some later Popen.
                    p.wait(timeout=2)
                except Exception:
                    pass

    def _read_frames(self, proc, width, height):
        frame_bytes = width * height * 3
        batch_frames = self.interval * self.FPS
        SENTINEL = object()

        def drain(q):
            while True:
                item = q.get()
                if item is SENTINEL:
                    return
                yield item

        def force_sentinel(q):
            # The consumer blocks in drain() until it sees SENTINEL; it MUST
            # be delivered on every exit path or the consumer thread — and
            # the reactor-side join waiting on it — hangs forever. If the
            # queue is full of unconsumed frames, discard one to make room.
            while True:
                try:
                    q.put_nowait(SENTINEL)
                    return
                except Full:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass

        batch_q = None
        n_in_batch = 0
        try:
            while True:
                buf = proc.stdout.read(frame_bytes)
                if len(buf) < frame_bytes:
                    break
                if self.dead:
                    continue  # keep the pipe drained so ffmpeg can exit
                if batch_q is None:
                    # Announce the batch as it STARTS so the consumer streams
                    # it live instead of waiting 10s for a complete list.
                    batch_q = queue.Queue(maxsize=batch_frames + 1)
                    self.sink(drain(batch_q))
                    n_in_batch = 0
                frame = np.frombuffer(buf, dtype=np.uint8).reshape(
                    (height, width, 3))
                try:
                    batch_q.put_nowait((n_in_batch / float(self.FPS), frame))
                except Full:
                    pass  # consumer lagging: analytics samples, never blocks
                n_in_batch += 1
                if n_in_batch >= batch_frames:
                    force_sentinel(batch_q)
                    batch_q = None
                    self.batches_done += 1
                    if self.batches_done % 6 == 1:
                        logging.info(
                            'analytics decoder for %s delivered batch %d',
                            self.label, self.batches_done)
        finally:
            # try/finally: an exception here (e.g. raised out of self.sink)
            # previously killed the reader with no sentinel, leaving the
            # consumer blocked forever and wedging teardown.
            if batch_q is not None:
                force_sentinel(batch_q)


cartoon_model = VideoCartoonifyLoader()
facial_emotion_detector = EmotionDetectionModel()
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
    facial_emotion_detector_V1 = EmotionDetectionModelV2()
else:
    facial_emotion_detector_V1 = EmotionDetectionModelV1()
image_object_detection = ImageObjectDetection(STOP_SIGNAL)
video_metric_analytics = VideoMetricAnalytics(attention_detection, facial_emotion_detector_V1, image_object_detection,STOP_SIGNAL)


class ServerProtocol(WsMessageMixin, WebSocketServerProtocol):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = None
        self.running = False
        self.processor = None
        self.cartoon_model = cartoon_model
        self.facial_emotion_detector = facial_emotion_detector_V1 #facial_emotion_detector
        self.image_object_detection = image_object_detection
        self.attention_detection = attention_detection
        self.video_processor = None
        self.last_message = time.time()
        self.end_signaled = False
        self.interval = 10
        self.stream_data = False
        self.awaitingSpeakers = True
        self.facial_embeddings = None
        # Per-pod toggle from the join form: when off, this connection only
        # records — no frame decode, no VideoProcessor. Default on (hardware
        # pods and older clients don't send the field).
        self.live_analytics = True
        # Silent-video detection: a pod can hold this socket open, heartbeat
        # normally, and never send a single video byte (a stalled client-side
        # capture). Audio keeps recording, so nothing looks wrong until
        # someone inspects the disk hours later. Track first-byte arrival and
        # complain loudly if it never comes.
        self.bytes_received = 0
        self.silent_check = None
        # Lag detection: under a saturated uplink a pod keeps the socket
        # open and sends chunks in bursts minutes apart — the recording
        # looks alive but is quietly falling behind, and everything still
        # queued on the phone dies with the tab at teardown. Track chunk
        # arrivals against wall time and warn the pod while there is still
        # time to fix it (move the phone, lower the resolution).
        self.binary_chunks = 0
        self.first_binary_at = None
        self.lag_check = None
        self.lag_warned = False
        self.chunk_decoder = None
        cm.add(self)
        cancel_idle_recycle()
        logging.info('New client connected...')

    SILENT_VIDEO_SECONDS = 30

    def arm_silent_check(self):
        self.cancel_silent_check()
        try:
            self.silent_check = reactor.callLater(
                self.SILENT_VIDEO_SECONDS, self._silent_video_fire)
        except Exception as e:
            logging.debug('could not arm silent-video check: %s', e)

    def cancel_silent_check(self):
        if self.silent_check is not None:
            try:
                if self.silent_check.active():
                    self.silent_check.cancel()
            except Exception:
                pass
            self.silent_check = None

    LAG_CHECK_SECONDS = 30
    LAG_ALERT_BEHIND_SECONDS = 45

    def arm_lag_check(self):
        self.cancel_lag_check()
        try:
            self.lag_check = reactor.callLater(
                self.LAG_CHECK_SECONDS, self._lag_check_fire)
        except Exception as e:
            logging.debug('could not arm lag check: %s', e)

    def cancel_lag_check(self):
        if self.lag_check is not None:
            try:
                if self.lag_check.active():
                    self.lag_check.cancel()
            except Exception:
                pass
            self.lag_check = None

    def _lag_check_fire(self):
        self.lag_check = None
        try:
            # Zero-bytes-ever is the silent-video alarm's case, not ours.
            if self.bytes_received > 0 and self.first_binary_at is not None:
                elapsed = time.time() - self.first_binary_at
                expected = elapsed / float(self.interval)
                behind = (expected - self.binary_chunks) * float(self.interval)
                key = self.config.auth_key if self.config else 'unknown'
                if behind > self.LAG_ALERT_BEHIND_SECONDS:
                    logging.error(
                        'VIDEO LAGGING: pod %s upload is ~%ds behind '
                        '(%d chunks in %ds; one expected every %ds). '
                        'Likely a saturated or weak uplink; footage still '
                        'queued on the phone is lost if the page closes '
                        'before it drains.',
                        key, int(behind), self.binary_chunks, int(elapsed),
                        int(self.interval))
                    self.send_json({
                        'type': 'video_lagging',
                        'message': 'Weak Wi-Fi: this device\'s video upload '
                                   'is about {0} seconds behind. Audio is '
                                   'unaffected. Keep this page open, and if '
                                   'possible move closer to the Wi-Fi access '
                                   'point.'.format(int(behind)),
                    })
                    self.lag_warned = True
                    _lagging.add(key)
                elif self.lag_warned:
                    logging.info('video lag cleared for pod %s', key)
                    self.send_json({'type': 'video_lag_cleared'})
                    self.lag_warned = False
                    _lagging.discard(key)
        finally:
            if not self.end_signaled:
                self.arm_lag_check()

    def _silent_video_fire(self):
        self.silent_check = None
        if self.bytes_received > 0:
            return
        key = self.config.auth_key if self.config else 'unknown'
        logging.error(
            'NO VIDEO DATA: pod %s has sent 0 video bytes %ds after starting '
            '(socket open, client alive). Audio is unaffected; this pod is '
            'recording no video. The client should reload and rejoin.',
            key, self.SILENT_VIDEO_SECONDS)
        # Tell the client too, so the phone can show it rather than looking fine.
        self.send_json({
            'type': 'no_video_data',
            'message': 'The server is receiving no video from this device. '
                       'Reload the page and rejoin to restore video recording.',
        })

    # onMessage / onClose come from WsMessageMixin (shared across both services).

    def process_json(self, data):
        if not 'type' in data:
            logging.warning('Message does not contain "type".')
            return
        
        if data['type'] == 'speaker':
            # Speakers can be added after the session starts, in which case
            # the start message carried numSpeakers=0 and facial_embeddings
            # was never initialized.
            if self.facial_embeddings is None:
                self.facial_embeddings = dict()
            if data['id'] == "done":
                self.awaitingSpeakers = False
                for speaker in data['speakers']:
                    self.facial_embeddings[speaker["id"]]["alias"] = speaker["alias"]
                if self.video_processor and (self.config.videocartoonify or self.config.video) and (cf.video_cartoonize() or cf.process_video_analytics()):
                    self.video_processor.setParticpantFacialEmbeddings(self.facial_embeddings)
                logging.info("Done awaiting all speakers info")
            else:
                self.currSpeaker = data['id']
                self.currAlias = data['alias']
                logging.info("preparing for speaker {}'s fingerprint".format(self.currSpeaker))

        if data['type'] == 'add-saved-fingerprint':
            currSpeaker = data['id']
            currAlias = data.get('alias')  # raw, for logging; validated below
            try:
                # Sanitize before it builds a path: np.load(allow_pickle=True)
                # on an attacker-chosen path is an RCE primitive.
                currAlias = safe_names.safe_name(data['alias'])
                facial_embedding_file = os.path.join(cf.facial_embedding_folder(), "{0}".format(currAlias))
                facials = np.load(facial_embedding_file+".npy", allow_pickle=True).item()
                if self.facial_embeddings is None:
                    self.facial_embeddings = dict()
                self.facial_embeddings[currSpeaker] = {"alias": currAlias, "data": facials[currAlias]}
                logging.info("storing registered speaker {}'s fingerprint with alias {}".format(currSpeaker, currAlias))
                self.send_json({'type':'registeredfingerprintadded'})
            except Exception as e:
                logging.info("error loading facial embedding for {} : {}".format(currSpeaker,e))
                # The client waits for a reply; silence leaves the join
                # dialog stuck on "processing".
                self.send_json({'type': 'registeredfingerprintfailed',
                                'message': 'No saved face fingerprint found for this username.'})
            
        if data['type'] == 'heartbeat':
            auth_key = data.get('key', None)
            logging.info("Recieved Heartbeat from client with authkey {0}".format(auth_key))
            if auth_key != "no key" and self.config is not None:
                # config-None guard: a heartbeat before 'start' raised
                # AttributeError into the log on every beat.
                if (self.config.videocartoonify or self.config.video) and not (self.live_analytics and (cf.video_cartoonize() or cf.process_video_analytics())):
                    self.send_json({'type':'heartbeat'})
                    logging.info("Sent Heartbeat response to client with authkey {0}".format(auth_key))

        if data['type'] == 'start':
            if self.config is not None:
                # config is set by every successful start. A second 'start'
                # on a live socket used to replace the decoder/processor/
                # recorders without stopping the old ones, leaking a live
                # ffmpeg and three threads until recycle.
                logging.warning('duplicate start ignored for %s', self.config.auth_key)
                self.send_json({'type': 'error', 'message': 'Processing already started on this connection.'})
                return
            valid, result = ProcessingConfig.from_json(data)
            if not valid:
                self.send_json({'type': 'error', 'message': "Initialization failed"})
                self.signal_end()
            else:
                self.config = result
                cm.associate_keys(self, self.config.session_key, self.config.auth_key)
                self.stream_data = data['streamdata']
                self.live_analytics = bool(data.get('liveAnalytics', True))
                if not self.live_analytics:
                    logging.info('Live analytics disabled by join form for {0} (record-only)'.format(self.config.auth_key))

                if(data['numSpeakers'] != 0):
                    self.facial_embeddings = dict()

                self.video_count = 1
                if cf.video_record_original():
                    aud_filename = os.path.join(cf.video_recordings_folder(), "{0}_{1}_{2}_({3})_audio".format(self.config.auth_key,self.config.sessionId,self.config.deviceId, str(time.ctime())))
                    self.filename = os.path.join(cf.video_recordings_folder(), "{0}_{1}_{2}_({3})_orig".format(self.config.auth_key,self.config.sessionId,self.config.deviceId, str(time.ctime())))
                    self.filename_remux = os.path.join(cf.video_recordings_folder(), "{0}_{1}_{2}_({3})_remux".format(self.config.auth_key,self.config.sessionId,self.config.deviceId, str(time.ctime())))

                    self.frame_dir = os.path.join(cf.video_recordings_folder(), "vid_img_frames_{0}_{1}_{2}_({3})".format(self.config.auth_key,self.config.sessionId,self.config.deviceId,  str(time.ctime())))
                    self.orig_vid_recorder = VidRecorder(self.filename,aud_filename,self.frame_dir,cf.video_record_original(),16000, 2, 1,self.config.mimeExtension)
                    
                    if (self.config.videocartoonify or cf.process_video_analytics()) and self.live_analytics:
                        self.video_queue = queue.Queue(maxsize=4)#maxsize=2
                        self.frame_queue = None
                        self.cartoon_image_queue = None

                        self.video_processor = VideoProcessor(self.cartoon_model,self.facial_emotion_detector,self.image_object_detection,self.attention_detection, \
                                                                self.video_queue,self.frame_queue,self.cartoon_image_queue,self.config,cf,self.frame_dir,self.filename,aud_filename,16000, 2,1,self.interval)
                        self.video_processor.add_websocket_connection(self)
                        # Incremental decode of the live stream, replacing
                        # the per-chunk whole-file moviepy reopen that
                        # starved the reactor (see StreamingChunkDecoder).
                        self.chunk_decoder = StreamingChunkDecoder(
                            self.interval, self.enqueue_latest_video_chunk,
                            self.config.auth_key)
                        if cf.video_cartoonize():
                            logging.warning(
                                'cartoonify audio sidecar is not produced by '
                                'the streaming decoder path')
                        
                if cf.video_record_reduced():
                    aud_filename = os.path.join(cf.video_recordings_folder(), "{0}_{1}_{2}_({3})_audio".format(self.config.auth_key,self.config.sessionId,self.config.deviceId, datetime.today().strftime("%Y-%m-%d")))
                    self.filename = os.path.join(cf.video_recordings_folder(), "{0}_{1}_{2}_({3})_redu".format(self.config.auth_key,self.config.sessionId,self.config.deviceId, datetime.today().strftime("%Y-%m-%d")))
                    self.frame_dir = os.path.join(cf.video_recordings_folder(), "vid_img_frames_{0}_{1}_{2}_({3})".format(self.config.auth_key,self.config.sessionId,self.config.deviceId, datetime.today().strftime('%Y-%m-%d')))
                    self.redu_vid_recorder = VidRecorder(self.filename,aud_filename,self.frame_dir,cf.video_record_original(),16000, 2, 1,self.config.mimeExtension)

                if (self.config.videocartoonify or self.config.video) and not (self.live_analytics and (cf.video_cartoonize() or cf.process_video_analytics())):
                    self.running = True
                    # Record-only connections skip signal_start, but they
                    # record video all the same — they need the silent-video
                    # and lag watchdogs too.
                    self.arm_silent_check()
                    self.arm_lag_check()
                    self.send_json({'type':'start','message':'Video processing not activated to start video processor'})
                    logging.info('Video process connected but video processing not activated')
                    reactor_safety.defer_blocking(callbacks.post_connect, self.config.auth_key)
                else:
                    self.signal_start()
                    self.send_json({'type':'start', 'message':'Video processing started'})
                    logging.info('Video process connected')
                    reactor_safety.defer_blocking(callbacks.post_connect, self.config.auth_key)

    def process_binary(self, data):
        if not self.bytes_received:
            self.cancel_silent_check()
            self.first_binary_at = time.time()
            logging.info('first video bytes from %s',
                         self.config.auth_key if self.config else 'unknown')
        self.bytes_received += len(data)
        _rx_total['bytes'] += len(data)
        if self.running and not self.awaitingSpeakers:
            self.binary_chunks += 1
            # if cf.video_record_original():
            if self.config.mimeExtension == "webm":
                self.orig_vid_recorder.write(data,self.filename+'.'+self.config.mimeExtension)
                logging.info('i just appended video data  for {0} to file'.format(self.config.auth_key))

            elif self.config.mimeExtension == "mp4":
                # temp_file_name = self.filename_remux+"_"+str(self.video_count)+"."+self.config.mimeExtension
                # self.orig_vid_recorder.write(data,temp_file_name)
                fixed = self.filename+"_"+str(self.video_count)+"."+self.config.mimeExtension
                self.orig_vid_recorder.write(data,fixed)
                # self.remux_mp4(temp_file_name, fixed)

                # Live frame analytics for mp4 pods: same streaming decoder
                # as webm — fragmented mp4 decodes from a pipe just as well,
                # and the init segment (ftyp) is the header the decoder
                # detects to (re)spawn ffmpeg.
                if self.chunk_decoder is not None:
                    self.chunk_decoder.feed(data)

            if self.config.mimeExtension == "webm":
                # Live analytics: hand the raw blob to the streaming
                # decoder. Constant cost per chunk and nothing on the
                # reactor thread — the old whole-file moviepy reopen here
                # grew ~0.25s per recorded MB and starved ingest for every
                # connected pod.
                if self.chunk_decoder is not None:
                    self.chunk_decoder.feed(data)

                self.video_count = self.video_count + 1
            
        else:
            self.send_json({'type': 'error', 'message': 'Binary audio data sent before start message.'})

    
    def enqueue_latest_video_chunk(self, chunk, timeout=0.05):
        """
        Keep only the most recent chunk in the queue.
        If the queue is full, remove the stale queued chunk and replace it.
        """
        try:
            self.video_queue.put(chunk, timeout=timeout)
            return True
        except Full:
            pass

        # Queue is full: drop the old queued item and release its clip
        try:
            old_item = self.video_queue.get_nowait()
            # optional: if you use task_done semantics elsewhere, call task_done here
            # self.video_queue.task_done()
        except Empty:
            old_item = None
        if old_item is not None and hasattr(old_item, 'close'):
            try:
                old_item.close()
            except Exception:
                pass

        # Try again to insert the latest chunk
        try:
            self.video_queue.put_nowait(chunk)
            logging.debug("Replaced stale video chunk with latest chunk.")
            return True
        except Full:
            logging.warning("Could not enqueue latest video chunk; dropping it.")
            if hasattr(chunk, 'close'):
                try:
                    chunk.close()
                except Exception:
                    pass
            return False
    
    def send_json(self, message):
        # The one sanctioned transport-write path (reactor-safe, best-effort).
        # This was a raw sendMessage — unsafe from the analytics/callback
        # worker threads that call it. See common/reactor_safety.
        reactor_safety.send_json(self, message)

    # PCM helpers live in src/common/audio_bytes.py, shared with the audio
    # servers (the local copy of reduce_wav_channel had lost its return).
    def read_bytes_from_wav(self,wav):
        return audio_bytes.read_bytes_from_wav(wav)

    def reduce_wav_channel(self,channels_wanted,wav):
        return audio_bytes.reduce_wav_channel(channels_wanted, wav, self.config.channels)


    def signal_start(self):
        # Recording has begun as far as the client is concerned — from here a
        # pod with nothing to send is a fault, not a slow start.
        self.arm_silent_check()
        self.arm_lag_check()
        if self.video_processor and (cf.video_cartoonize() or cf.process_video_analytics()):
            self.video_processor.start()
            self.running = True

    def send_close(self, message):
        self.send_json({'type': 'end', 'message': message})

    def signal_end(self):
        if self.end_signaled:
            return
        self.end_signaled = True
        self.cancel_silent_check()
        self.cancel_lag_check()
        if self.config:
            _lagging.discard(self.config.auth_key)
        if self.config and self.bytes_received == 0:
            logging.error('pod %s disconnected having sent NO video data at all',
                          self.config.auth_key)

        if self.chunk_decoder is not None:
            self.chunk_decoder.stop()

        # The worker join runs off the reactor (audit B.7); stop() returns
        # its Deferred, or None when there was nothing to join.
        stopping = None
        if  self.video_processor:
            stopping = self.video_processor.stop()
            logging.info("Video processor stopping for client {0}".format(self.config.auth_key))

        if self.config:
            # 30s-timeout HTTP off the reactor; teardown must not stall ingest.
            reactor_safety.defer_blocking(callbacks.post_disconnect, self.config.auth_key)
            cm.remove(self, self.config.session_key, self.config.auth_key)
            # Live pods never send last_batch, so their analytics queues are
            # never popped by the schedulers — evict here or a disconnected
            # pod's queued frame payloads stay pinned until process recycle.
            # Sequenced AFTER the worker join: a last late batch from the
            # worker would otherwise re-create the queue we just popped.
            key = self.config.auth_key

            def _evict(_result=None):
                image_object_detection.frame_queue_manager.pop(key, None)
                image_object_detection.accumulator_queue_manager.pop(key, None)

            if stopping is not None:
                stopping.addErrback(lambda f: logging.warning(
                    'video processor join for %s failed: %s', key, f.getErrorMessage()))
                stopping.addCallback(_evict)
            else:
                _evict()
        else:
            cm.remove(self, None, None)

        if cm.get_number_of_connections() == 0:
            # Deliberately NOT stopping image_object_detection /
            # video_metric_analytics here: stop() permanently exits their
            # worker threads and the only start() calls are in __main__, so a
            # class joining inside the idle-recycle window would get no
            # analytics at all (frames enqueue, nothing drains). Idle threads
            # cost a 50ms poll; the idle recycle below reclaims everything.
            logging.info("No more connected clients; scheduling idle recycle.")
            schedule_idle_recycle()
        logging.info('Closing client connection...')
        self.transport.loseConnection()

        # Begin Post Processing
        if cf.video_record_original() and self.stream_data == 'video':
            self.orig_vid_recorder.close(self.filename)

if __name__ == '__main__':
    cf.initialize()
    # Initialize Logger
    dir_path = os.path.dirname(os.path.realpath(__file__))
    log_format = logging.Formatter('%(asctime)s - %(levelname)s: %(message)s')
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    log_file = logging.FileHandler(os.path.join(dir_path, 'video_processing_service.log'))
    log_file.setFormatter(log_format)
    logger.addHandler(log_file)
    log_console = logging.StreamHandler()
    log_console.setFormatter(log_format)
    logger.addHandler(log_console)

    # Initialize cartoonify
    if cf.video_cartoonize():
        cartoon_model.load_model()

    # Initialize attention  and emotion tracking
    if cf.process_video_analytics():
        image_object_detection.init_model(cartoon_model.batch_size) #get_object_detector(_create_detector)
        attention_detection.init_model(cartoon_model.batch_size) #get_attention_emotion_predictor(_create_gaze_predictor, _create_emotion_predictor)[0]
        facial_emotion_detector_V1.load_model() 
        image_object_detection.start()
        video_metric_analytics.start() 
    

    # Run Server
    logging.info('Starting video Processing Service...')
    poll_connections = task.LoopingCall(cm.check_connections)
    poll_connections.start(10.0)
    auth_connections = task.LoopingCall(cm.check_connection_authentication)
    auth_connections.start(5.0)
    throughput_gauge = task.LoopingCall(_throughput_gauge)
    throughput_gauge.start(GAUGE_SECONDS, now=False)
    factory = WebSocketServerFactory()
    # Keepalive + payload cap (audit B.6): without pings a dead peer held its
    # ffmpeg and threads until the 300 s inactivity sweep.
    factory.setProtocolOptions(autoPingInterval=10, autoPingTimeout=20,
                               maxMessagePayloadSize=MAX_WS_MESSAGE_BYTES)
    factory.protocol = ServerProtocol
    reactor.listenTCP(int(os.environ.get("DC_VIDEO_WS_PORT", 9003)), factory)
    logging.info('Video Processing Service started.')
    reactor.run()
    logging.info('Video Processing Service ended.')