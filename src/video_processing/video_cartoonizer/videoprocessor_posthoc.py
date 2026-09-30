import os as _rs_os, sys as _rs_sys  # noqa: E401
_rs_c = _rs_os.path.dirname(_rs_os.path.abspath(__file__))
while _rs_c != '/' and not _rs_os.path.isdir(_rs_os.path.join(_rs_c, 'common')):
    _rs_c = _rs_os.path.dirname(_rs_c)
_rs_c = _rs_os.path.join(_rs_c, 'common')
if _rs_c not in _rs_sys.path:
    _rs_sys.path.insert(0, _rs_c)
import reactor_safety  # reactor/thread boundary; src/common bootstrapped above
# Imported as video_cartoonizer.videoprocessor_posthoc, so add this file's own
# dir for the bare sibling import below.
_rs_sys.path.insert(0, _rs_os.path.dirname(_rs_os.path.abspath(__file__)))
from frame_payload import build_frame_payload
import logging
import threading
import time
import config as cf
import traceback
try:
    import moviepy.editor as mp  # moviepy 1.x
except ImportError:
    import moviepy as mp  # moviepy 2.x dropped the .editor module
import os
import warnings
import cv2
import tempfile
import subprocess

from pathlib import Path
from queue import Full

WAIT_TIMEOUT = 0.05   # 50 ms (tune this)

class VideoProcessorPosthoc:
    def __init__(self,facial_emotion_detector,image_object_detection,attention_detection,video_metric_analytics,video_file,config,running_video_processes,video_interval,batch_size,vid_img_dir,STOP_SIGNAL,save_gaze_annotation=False):
        
        self.facial_emotion_detector = facial_emotion_detector
        self.image_object_detection = image_object_detection
        self.attention_detection = attention_detection
        self.video_metric_analytics = video_metric_analytics
        self.video_file = video_file
        self.config = config
        self.running_video_processes = running_video_processes
        self.video_interval = video_interval
        self.STOP = STOP_SIGNAL
        self.batch_size = batch_size
        self.frame_batch = []
        self.time_marker = []
        self.vid_img_dir = vid_img_dir

        self.frame_count = 1
        self.frame_array = []
        self.lock = threading.Lock()
        self.batch_track = 0
       
        self.web_socket_connection = None
        self.facialEmbeddings = None
        self.video_chunk_count = -1

        if save_gaze_annotation and not os.path.exists(self.vid_img_dir):
            os.mkdir(self.vid_img_dir)
       
    def start(self):
        self.running = True
        self.cancelled = False
        self.preempted = False
        self._error = None
        self._started_at = time.time()  # epoch; sent to clients so elapsed survives refresh
        self.vid_pro_thread = threading.Thread(target=self.processing, name="video_posthoc_processing")
        self.vid_pro_thread.daemon = True
        self.vid_pro_thread.start()

    def stop(self):
        self.running = False
        # self.vid_pro_thread.join()

    def cancel(self):
        # The loop stops at its next chunk; the finally below reports the
        # run as not completed (posthoc_failed) instead of completed.
        self.cancelled = True
        self.running = False

    def preempt(self):
        # A live class started (PreemptionWatcher): cancel path, reported as
        # 'preempted' so the queue re-runs the pod when the class ends.
        self.preempted = True
        self.cancel()

    def outcome(self):
        """None when the run completed; else why it did not."""
        if self.preempted:
            return 'preempted'
        if self.cancelled:
            return 'cancelled'
        return self._error

    def add_websocket_connection(self,web_socket):
        self.web_socket_connection = web_socket
        # Replay the latest progress so a reconnecting client sees % immediately.
        if getattr(self, '_last_progress', None):
            self.send_json(self._last_progress)

    def setParticpantFacialEmbeddings(self,facialEmbeddings):
        self.facialEmbeddings = facialEmbeddings

    def send_json(self, message):
        # The run may outlive the triggering client (background completion),
        # so a closed socket is fine — reactor_safety.send_json is best-effort.
        if self.web_socket_connection is None:
            return
        reactor_safety.send_json(self.web_socket_connection, message)

    def _probe_duration(self):
        # Browser-captured webm files often carry no duration metadata; both
        # moviepy AND ffprobe then report a bogus 86400s (24h) placeholder.
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", self.video_file],
                capture_output=True, timeout=30)
            val = float(out.stdout.decode().strip())
            if 0 < val < 86000:  # 86400 is the "unknown" placeholder — distrust it
                return val
        except Exception:
            pass
        # Authoritative fallback: demux (no decode) and read the last packet
        # timestamp from ffmpeg's progress output. Fast even for long files.
        try:
            import re
            out = subprocess.run(
                ["ffmpeg", "-v", "info", "-i", self.video_file,
                 "-c", "copy", "-f", "null", "-"],
                capture_output=True, timeout=180)
            times = re.findall(rb"time=(\d+):(\d+):(\d+(?:\.\d+)?)", out.stderr)
            if times:
                h, m, s = times[-1]
                val = int(h) * 3600 + int(m) * 60 + float(s)
                return val if val > 0 else None
        except Exception:
            pass
        return None
        

    def adjust_time(self,frame_sec_mark):
        offset = self.config.start_offset
        return round(offset+ (frame_sec_mark + (self.video_interval * self.video_chunk_count)))
    
    def processing(self):
        try:
            #uncomment later
            # if self.image_object_detection.running == False:
            #     self.image_object_detection.start()

            # if self.video_metric_analytics.running == False:
            #     self.video_metric_analytics.start()

            with tempfile.TemporaryDirectory() as tmpdir:
                logging.info("temp dir is {0}".format(tmpdir))
                warming_error_count  = 0 
                vidclip = mp.VideoFileClip(self.video_file)
                # Correct moviepy's 24h guess for duration-less webm captures.
                _probed = self._probe_duration()
                _duration = vidclip.duration
                if _probed and (_duration is None or _duration >= 86000 or _duration > _probed * 1.5):
                    logging.info("duration corrected: moviepy %s -> ffprobe %.1fs", _duration, _probed)
                    _duration = _probed

                current_dir = Path(__file__).resolve().parent
                # Go up two level (parent)
                parent_dir = current_dir.parent.parent
                # Go into sibling directory
                target_dir = parent_dir / "video_processing" / "videorecordings"
                final_audio_file = os.path.join(target_dir, "{0} ({1})_audio.wav".format(self.config.auth_key, str(time.ctime())))
                chunk_files = []
                for start in range(0, int(_duration), self.video_interval):
                    end = min(start + self.video_interval, _duration)
                    dur = end-start
                    # Live progress to the trigger UI (position in the video).
                    try:
                        _pct = round(100.0 * start / max(1, _duration))
                        self._last_progress = {'type': 'progress',
                                               'message': 'Analyzing video {0}s / {1}s'.format(int(start), int(_duration)),
                                               'percent': _pct,
                                               'started_at': getattr(self, '_started_at', None)}
                        self.send_json(self._last_progress)
                    except Exception:
                        pass

                    if self.running == False:
                        logging.info("Video processor posthoc thread stop initiated")
                        break
                    try:
                        with warnings.catch_warnings():
                            warnings.simplefilter("error", UserWarning)

                            subclip = (vidclip.subclipped if hasattr(vidclip, 'subclipped') else vidclip.subclip)(start, end)  # moviepy 2 rename

                            #use this to extract audio incase of incomplete audio in the original audio file
                            chunk_path = os.path.join(tmpdir, f"chunk_{start:04d}.wav")

                            subprocess.run([
                                            "ffmpeg",
                                            "-y",
                                            "-ss", str(start),
                                            "-t", str(dur),
                                            "-i", self.video_file,
                                            "-vn",
                                            "-ar", "16000",
                                            "-ac", "1",
                                            "-acodec", "pcm_f32le",
                                            chunk_path
                                        ], check=True)
                            chunk_files.append(chunk_path)

                            self.video_chunk_count+=1
                            subclib_frame_count = 0
                            for ts, frame in subclip.iter_frames(fps=cf.posthoc_fps(), dtype="uint8", with_times=True):
                                subclib_frame_count =  subclib_frame_count + 1
                                # self.frame_batch.append(frame)
                                self.frame_batch.append(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                                self.time_marker.append(self.adjust_time(ts))
                                # logging.info("frame size is {0}, dtype is {1}, min is {2}, max is {3}".format(frame.shape, frame.dtype, frame.min(), frame.max()))
                                if len(self.frame_batch) == self.batch_size:
                                    logging.info(f"batch being inserted is {self.batch_track}")
                                    payload = build_frame_payload(self.frame_batch, self.facialEmbeddings, self.batch_track, self.time_marker, self.vid_img_dir, self.config.auth_key, False)
                                    accumulator_load = self.image_object_detection.worker_posthoc(payload)  
                                    self.video_metric_analytics.worker(accumulator_load, post_always=True)
                                
                                    self.frame_batch = []
                                    self.time_marker = []
                                
                                    self.batch_track += 1
                                #if i read 300 frames from this chunk then break
                                if subclib_frame_count == 300:
                                    break

                            subclip.close() 
                    except UserWarning as e:
                        warming_error_count += 1
                        logging.info(f"Frame read warning turned error: {e}")
                        if warming_error_count > 2:
                            break
                            # self.stop()

                # push the rest of the batched frames adjtime to queue
                payload = build_frame_payload(self.frame_batch, self.facialEmbeddings, self.batch_track, self.time_marker, self.vid_img_dir, self.config.auth_key, False)
                
                accumulator_load = self.image_object_detection.worker_posthoc(payload)  
                self.video_metric_analytics.worker(accumulator_load, post_always=True)

                # create ffmpeg concat list
                list_file = os.path.join(tmpdir, "chunks.txt")
                with open(list_file, "w") as f:
                    for path in chunk_files:
                        f.write(f"file '{path}'\n")

                # concatenate into one final wav
                subprocess.run([
                                "ffmpeg",
                                "-y",
                                "-f", "concat",
                                "-safe", "0",
                                "-i", list_file,
                                "-ar", "16000",
                                "-ac", "1",
                                "-acodec", "pcm_f32le",
                                final_audio_file
                            ], check=True)
                vidclip.close()
        except Exception as e:
            error_str = traceback.format_exc()
            self._error = '%s: %s' % (type(e).__name__, e)
            logging.warning('Exception thrown while Processing video posthoc video image extraction {0} {1}'.format(error_str, self.config.auth_key))
        finally:
                try:
                    logging.info("called finally after thread stop initiated")
                    failure = self.outcome()
                    self.running_video_processes.pop(self.config.auth_key,None)
                    # The last_batch flush also unregisters this pod's queue
                    # state downstream, so it runs on every outcome.
                    payload = build_frame_payload(self.frame_batch, self.facialEmbeddings, self.batch_track, self.time_marker, self.vid_img_dir, self.config.auth_key, True)

                    accumulator_load = self.image_object_detection.worker_posthoc(payload)
                    self.video_metric_analytics.worker(accumulator_load, post_always=True)

                    logging.info('Video Processor Posthoc  stopped for {0}.'.format(self.config.auth_key))
                    if failure is None:
                        self.send_json({'type': 'process_completed', 'message': "Video posthoc analytics completed"})
                    elif failure == 'preempted':
                        from gpu_lease import PREEMPTED_REPLY  # src/common
                        self.send_json(dict(PREEMPTED_REPLY))
                    else:
                        logging.warning('Video post-hoc run for %s did not complete: %s', self.config.auth_key, failure)
                        self.send_json({'type': 'error', 'code': 'posthoc_failed',
                                        'message': 'Video analysis %s.' % ('cancelled' if failure == 'cancelled' else 'failed (%s)' % failure)})
                    # Mark complete/failed server-side so it persists even if
                    # the triggering browser disconnected.
                    try:
                        on_finished = getattr(self, 'on_finished', None)
                        if on_finished is None and self.web_socket_connection is not None and hasattr(self.web_socket_connection, 'on_run_complete'):
                            on_finished = self.web_socket_connection.on_run_complete
                        if on_finished is not None:
                            on_finished(success=failure is None, reason=failure)
                    except Exception as ex:
                        logging.warning("video on_run_complete notify failed: %s", ex)
                except Full:
                    pass
        