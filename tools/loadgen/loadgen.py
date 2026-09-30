#!/usr/bin/env python3
"""Replay a recording into BLINC's live audio socket as N synthetic BYOD pods
and measure caption lag. Meant to run ON the server; see README.md.

Mirrors the browser BYOD client (frontend/src/byod-join/byod-join-component.jsx)
message for message; the pure protocol/plan/summary logic is loadgen_core.py.
"""
import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import loadgen_core as core  # noqa: E402

REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AUDIO_LOG = os.path.join(REPO, "src", "audio_processing", "audio_processing_service.log")
PRODUCTION_HOSTS = {"nublinc.com", "www.nublinc.com"}
POLL_SECONDS = 5.0
AUDIO_WS_PORT = 9010


def log(msg):
    print("%s %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- HTTP, the same routes the browser uses ----------------------------------
class ApiError(Exception):
    pass


class Api:
    def __init__(self, base_url):
        import requests
        self.base = base_url.rstrip("/") + "/"
        # Teacher: DCSession cookie set by /api/v1/login (routes/auth.py:51-56).
        self.teacher = requests.Session()
        # Pods have no cookie; X-Processing-Key proves the pod (api-service.js:70-80,
        # wrappers.py:151-158). Kept apart so the polls look exactly like a phone's.
        self.pod = requests.Session()

    def _call(self, sess, method, path, body=None, headers=None):
        r = sess.request(method, self.base + path, json=body, headers=headers, timeout=30)
        if r.status_code != 200:
            raise ApiError("%s %s -> %d %s" % (method, path, r.status_code, r.text[:200]))
        return r.json() if r.content else {}

    def login(self, email, password):
        return self._call(self.teacher, "POST", "api/v1/login", {"email": email, "password": password})

    def create_session(self, name):  # session-service.js:302-327, routes/session.py:195-228
        return self._call(self.teacher, "POST", "api/v1/sessions", {
            "name": name, "devices": [], "keywordListId": None, "topicModelId": None,
            "byod": True, "features": True, "doa": False, "folder": None, "asr": None})

    def join(self, passcode, alias):  # session-service.js:330-337, routes/session.py:230-247
        return self._call(self.pod, "POST", "api/v1/sessions/byod",
                          {"name": alias, "passcode": passcode, "collaborators": 0})

    def transcripts(self, device_id, key, after_id):  # session-service.js:196-202
        path = "api/v1/devices/%d/transcriptspeakermetrics/client" % device_id
        if after_id > 0:
            path += "?after_id=%d" % after_id
        return self._call(self.pod, "GET", path, headers={"X-Processing-Key": key})

    def end_session(self, session_id):  # session-service.js:33-39
        return self._call(self.teacher, "POST", "api/v1/sessions/%d/stop" % session_id, {})

    def delete_session(self, session_id):  # session-service.js:76-82
        return self._call(self.teacher, "DELETE", "api/v1/sessions/%d" % session_id)


# --- one synthetic pod --------------------------------------------------------
class Pod:
    def __init__(self, n, api, session, pcm, start_sample, ws_url, stop):
        self.n, self.alias = n, core.pod_alias(n)
        self.api, self.session, self.pcm, self.start_sample, self.ws_url, self.stop = \
            api, session, pcm, start_sample, ws_url, stop
        self.lock = threading.Lock()
        self.device_id = self.key = self.embeddings = self.creation_date = None
        self.ws, self.ready, self.ended, self.end_reason = None, False, False, None
        self.backlog, self.backlog_bytes = [], 0
        self._start_sent_at = None
        self._attempt, self._since = 0, None
        self.audio_sent_samples = self.conn_sent_samples = 0
        self.start_offset_s = self.latest_end_s = None
        self.transcripts_total = self.after_id = 0
        self.degraded_events = self.ok_events = self.reconnects = self.dropped_chunks = self.poll_errors = 0
        self.asr_state = "ok"
        self.threads = []
        self._capture_started = False

    def join(self):
        data = self.api.join(self.session["passcode"], self.alias)
        self.device_id = int(data["session_device"]["id"])
        self.key = data["key"]
        self.embeddings = data["session_device"].get("embeddings")
        # The join response's session comes from the DB: the same server_start
        # the audio service reads from Redis (session_handler.py:16).
        self.creation_date = core.parse_creation_date(data["session"]["creation_date"])

    def start(self):
        for target in (self._connection_loop, self._poll_loop):
            self._spawn(target)

    def _spawn(self, target):
        t = threading.Thread(target=target, name="%s-%s" % (self.alias, target.__name__), daemon=True)
        t.start()
        self.threads.append(t)

    # The audio clock: one frame every 0.256 s from the first start ack on,
    # whether or not the socket is up (the browser streams only after the
    # ack, :2032-2034, and its AudioContext keeps capturing across reconnects),
    # so the first start says start_time 0 and a re-sent one the backlog.
    def _capture_loop(self):
        t0, i = time.monotonic(), 0
        while not self.stop.is_set() and not self.ended:
            delay = core.chunk_deadline(t0, i) - time.monotonic()
            if delay > 0:
                self.stop.wait(delay)
            if self.stop.is_set():
                break
            self._send_frame(core.chunk_at(self.pcm, i, self.start_sample))
            i += 1

    def _ws_send(self, ws, chunk):
        try:
            ws.send_binary(chunk)
        except Exception:
            self.ws, self.ready = None, False
            return False
        self.audio_sent_samples += core.CHUNK_SAMPLES
        self.conn_sent_samples += core.CHUNK_SAMPLES
        return True

    def _send_frame(self, chunk):  # sendAudioFrame, byod-join-component.jsx:998-1017
        with self.lock:
            ws = self.ws if self.ready else None
            if ws is not None and not self.backlog:
                self._ws_send(ws, chunk)
                return
            self.backlog.append(chunk)
            self.backlog_bytes += len(chunk)
            while self.backlog_bytes > core.AUDIO_BACKLOG_CAP_BYTES and self.backlog:
                self.backlog_bytes -= len(self.backlog.pop(0))
                self.dropped_chunks += 1
            if ws is not None:
                self._drain_backlog(ws)

    def _drain_backlog(self, ws):  # drainAudioBacklog, :1020-1035 (lock held)
        while self.backlog and self.ready:
            chunk = self.backlog[0]
            if not self._ws_send(ws, chunk):
                return
            self.backlog.pop(0)
            self.backlog_bytes -= len(chunk)

    # scheduleReconnect, :910-958: full-jitter backoff 1 -> 30 s, 3 min deadline
    def _connection_loop(self):
        while not self.stop.is_set() and not self.ended:
            self._run_connection()
            if self.stop.is_set() or self.ended:
                break
            if self._since is None:
                self._since = time.monotonic()
            if time.monotonic() - self._since > core.RECONNECT_DEADLINE_S:
                self._end("reconnect deadline passed")
                break
            delay = core.backoff_delay(self._attempt)
            self._attempt += 1
            self.reconnects += 1
            log("%s: socket closed; reconnect attempt %d in %.1fs" % (self.alias, self._attempt, delay))
            self.stop.wait(delay)

    def _run_connection(self):
        import websocket
        ws = websocket.WebSocket()
        try:
            ws.connect(self.ws_url, timeout=core.RECONNECT_ATTEMPT_TIMEOUT_S)
        except Exception as e:
            log("%s: connect failed: %s" % (self.alias, e))
            return
        with self.lock:
            # start (:2148-2162) then the roster (:1387-1392), in that order,
            # on the same connection; the ack gates streaming (:2032-2034).
            start_time = core.backlog_seconds(self.backlog_bytes)
            self._start_sent_at = utcnow()
            self.conn_sent_samples = 0
            try:
                ws.send(json.dumps(core.start_message(self.key, self.device_id, self.session["id"],
                                                      self.embeddings, start_time)))
                ws.send(json.dumps(core.speaker_done_message()))
            except Exception as e:
                log("%s: start failed: %s" % (self.alias, e))
                return
            self.ws, self.ready = ws, False
        ws.settimeout(1.0)
        ack_deadline = time.monotonic() + core.RECONNECT_ATTEMPT_TIMEOUT_S
        try:
            while not self.stop.is_set() and not self.ended:
                try:
                    msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    if not self.ready and time.monotonic() > ack_deadline:
                        log("%s: no start ack in %.0fs; retrying" % (self.alias, core.RECONNECT_ATTEMPT_TIMEOUT_S))
                        break
                    continue
                except Exception:
                    break
                if not msg:
                    break
                self._on_message(msg)
        finally:
            with self.lock:
                self.ws, self.ready = None, False
            try:
                ws.close()
            except Exception:
                pass

    def _on_message(self, raw):  # audiows.onmessage, byod-join-component.jsx:2029-2064
        try:
            m = json.loads(raw)
        except (TypeError, ValueError):
            return
        kind = m.get("type")
        if kind == "start":
            with self.lock:
                self.ready = True
                # The service reads `offset` (not start_time), so it sees 0.
                self.start_offset_s = core.server_start_offset(self._start_sent_at, self.creation_date, 0.0)
                self._attempt, self._since = 0, None   # episode over, :317-325
                if not self._capture_started:
                    self._capture_started = True
                    self._spawn(self._capture_loop)
                if self.backlog and self.ws is not None:
                    self._drain_backlog(self.ws)
        elif kind == "asr_status":  # :2041-2048 {"state": "ok"|"degraded"}
            with self.lock:
                state = m.get("state")
                if state == "degraded" and self.asr_state != "degraded":
                    self.degraded_events += 1
                elif state == "ok" and self.asr_state == "degraded":
                    self.ok_events += 1
                self.asr_state = state or self.asr_state
        elif kind == "error":       # :2049-2055 permanent
            self._end("audio server error: %s" % m.get("message"))
        elif kind == "end":         # :2057-2063 permanent
            self._end("session ended by the server: %s" % m.get("message"))

    def _end(self, reason):
        self.ended, self.end_reason = True, reason
        log("%s: %s" % (self.alias, reason))

    def _poll_loop(self):  # every 5 s with after_id (session-service.js:27-30)
        while not self.stop.is_set():
            try:
                rows = list(core.transcript_rows(self.api.transcripts(self.device_id, self.key, self.after_id)))
            except Exception:
                self.poll_errors += 1
            else:
                with self.lock:
                    self.latest_end_s = core.latest_end(rows, self.latest_end_s)
                    self.after_id = core.max_id(rows, self.after_id)
                    self.transcripts_total += len(rows)
            self.stop.wait(POLL_SECONDS)

    def snapshot(self):
        with self.lock:
            sent_s = self.audio_sent_samples / core.SAMPLE_RATE
            clock = None if self.start_offset_s is None else self.start_offset_s + self.conn_sent_samples / core.SAMPLE_RATE
            lag = None if clock is None else core.caption_lag(clock, self.latest_end_s, self.start_offset_s)
            return {
                "pod": self.n, "alias": self.alias, "device_id": self.device_id,
                "connected": int(self.ws is not None), "ready": int(self.ready), "ended": int(self.ended),
                "reconnects": self.reconnects, "audio_sent_s": round(sent_s, 3),
                "audio_clock_s": None if clock is None else round(clock, 3),
                "start_offset_s": None if self.start_offset_s is None else round(self.start_offset_s, 3),
                "latest_transcript_end_s": self.latest_end_s,
                "lag_s": None if lag is None else round(lag, 3),
                "transcripts_total": self.transcripts_total, "degraded_events": self.degraded_events,
                "ok_events": self.ok_events, "asr_state": self.asr_state,
                "dropped_chunks": self.dropped_chunks, "poll_errors": self.poll_errors,
            }

    def close(self):
        with self.lock:
            ws, self.ws, self.ready = self.ws, None, False
        if ws is not None:
            try:
                ws.close()   # a pod leaving: just close, no `end` message (disconnect(), :873-893)
            except Exception:
                pass


# --- host metrics -------------------------------------------------------------
class HostSampler:
    def __init__(self, log_path):
        self.log_path = log_path
        self.offset = os.path.getsize(log_path) if os.path.exists(log_path) else 0
        self.drop_warnings = 0

    def sample(self):
        out = {"load1": round(os.getloadavg()[0], 2), "mem_free_mb": None, "gpu_util": None, "gpu_mem_mb": None}
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        out["mem_free_mb"] = int(line.split()[1]) // 1024
        except OSError:
            pass
        try:
            gpu = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
                                  "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
            util, mem = gpu.strip().splitlines()[0].split(",")
            out["gpu_util"], out["gpu_mem_mb"] = int(util), int(mem)
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            pass
        try:
            size = os.path.getsize(self.log_path)
            if size < self.offset:          # rotated (copytruncate)
                self.offset = 0
            with open(self.log_path, "rb") as f:
                f.seek(self.offset)
                self.drop_warnings += core.count_drop_warnings(f.read().decode("utf-8", "replace"))
                self.offset = f.tell()
        except OSError:
            pass
        out["drop_warnings"] = self.drop_warnings
        return out


# --- safety -------------------------------------------------------------------
def live_pods_on_box():
    """Real pods right now: Phase 2 presence keys and established connections into the audio service."""
    found = []
    for cmd, tag in ((["redis-cli", "--scan", "--pattern", "live_pod:*"], "redis "),
                     (["ss", "-Htn", "state", "established", "( dport = :%d )" % AUDIO_WS_PORT], "tcp ")):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        found += [tag + line.strip() for line in out.splitlines() if line.strip()]
    return found


def load_pcm(path):
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-f", "f32le", "-acodec", "pcm_f32le",
           "-ac", "1", "-ar", str(core.SAMPLE_RATE), "-"]
    pcm = subprocess.run(cmd, check=True, capture_output=True).stdout
    return bytes(pcm[:len(pcm) - len(pcm) % core.BYTES_PER_SAMPLE])


# --- main ---------------------------------------------------------------------
CSV_FIELDS = ["ts", "elapsed_s", "step", "pods_target", "step_elapsed_s", "pod", "alias", "device_id",
              "connected", "ready", "ended", "reconnects", "audio_sent_s", "audio_clock_s", "start_offset_s",
              "latest_transcript_end_s", "lag_s", "transcripts_total", "degraded_events", "ok_events",
              "asr_state", "dropped_chunks", "poll_errors", "load1", "mem_free_mb", "gpu_util", "gpu_mem_mb",
              "drop_warnings"]


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="https://nublinc.com")
    p.add_argument("--ws-url", help="audio socket URL if not <base-url as ws(s)>/audio_socket (test instances)")
    p.add_argument("--email", default=os.environ.get("BLINC_LOADGEN_EMAIL"))
    p.add_argument("--password", default=os.environ.get("BLINC_LOADGEN_PASSWORD"))
    p.add_argument("--audio", required=True, help="wav or anything ffmpeg reads; converted to 16 kHz mono f32")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--pods", type=int)
    g.add_argument("--ramp", help="cumulative pod counts per step, e.g. 2,4,8,12,16")
    p.add_argument("--step-seconds", type=float, default=180.0)
    p.add_argument("--session-name", default=core.default_session_name(datetime.now()))
    p.add_argument("--keep", action="store_true", help="do not end/delete the session at exit")
    p.add_argument("--dry-run", action="store_true", help="no network at all: print the plan")
    p.add_argument("--out", default=os.path.join("results", datetime.now().strftime("%Y%m%d-%H%M%S") + ".csv"))
    p.add_argument("--max-lag", type=float, default=30.0, help="verdict threshold on p95 lag, seconds")
    p.add_argument("--warmup-seconds", type=float, default=60.0, help="ignored at the start of each step for the verdict")
    p.add_argument("--i-understand-this-loads-production", action="store_true")
    p.add_argument("--check-live", action="store_true", help="run the live-pod checks even off the production host")
    return p.parse_args(argv)


def _raise_interrupt(*_):
    raise KeyboardInterrupt


def main(argv=None):
    args = parse_args(argv)
    try:
        steps = core.parse_ramp(args.ramp if args.ramp else str(args.pods))
    except ValueError as e:
        sys.exit("error: %s" % e)
    if not core.session_name_ok(args.session_name):
        sys.exit("error: --session-name must start with %s and use only letters, digits, spaces, ' and :"
                 % core.SESSION_PREFIX)
    url = urlparse(args.base_url)
    host = (url.hostname or "").lower()
    if host in PRODUCTION_HOSTS and not args.i_understand_this_loads_production:
        print("refusing: %s is production; pass --i-understand-this-loads-production (and never during a class)"
              % host, file=sys.stderr)
        return 2
    if host in PRODUCTION_HOSTS or args.check_live:
        live = live_pods_on_box()
        if live:
            print("refusing: real pods look live on this box:\n  " + "\n  ".join(live), file=sys.stderr)
            return 2
    pcm = load_pcm(args.audio)
    duration = core.total_samples(pcm) / core.SAMPLE_RATE
    if duration < core.CHUNK_SECONDS:
        sys.exit("error: recording shorter than one chunk")
    plan = core.ramp_plan(steps, args.step_seconds)
    offsets = core.pod_start_samples(steps[-1], core.total_samples(pcm))
    # api-service.js:44-51: same host as the page, /audio_socket, wss when https
    ws_url = args.ws_url or ("wss" if url.scheme == "https" else "ws") + "://" + url.netloc + "/audio_socket"

    log("audio: %s -> %.1f s of 16 kHz mono f32, %d-byte frames every %.3f s"
        % (args.audio, duration, core.CHUNK_BYTES, core.CHUNK_SECONDS))
    log("session: %r on %s (keep=%s), results -> %s" % (args.session_name, args.base_url, args.keep, args.out))
    for step, pods, new, t0, t1 in plan:
        log("step %d: %d pods (+%d) from %.0f s to %.0f s" % (step, pods, new, t0, t1))
    if args.dry_run:
        log("dry run: would POST api/v1/login (credentials %s), POST api/v1/sessions byod=true,"
            % ("set" if args.email and args.password else "NOT set"))
        for n in range(1, steps[-1] + 1):
            log("  pod %s: POST api/v1/sessions/byod collaborators=0; open %s; send %s then %s; "
                "stream from %.1f s into the recording; poll transcriptspeakermetrics/client?after_id= every %.0f s"
                % (core.pod_alias(n), ws_url, json.dumps(core.start_message("<key>", "<device>", "<session>")),
                   json.dumps(core.speaker_done_message()), offsets[n - 1] / core.SAMPLE_RATE, POLL_SECONDS))
        log("dry run: then POST api/v1/sessions/<id>/stop and DELETE api/v1/sessions/<id>%s"
            % (" (skipped: --keep)" if args.keep else ""))
        return 0
    if not (args.email and args.password):
        sys.exit("error: --email/--password or BLINC_LOADGEN_EMAIL/BLINC_LOADGEN_PASSWORD required")

    api = Api(args.base_url)
    api.login(args.email, args.password)
    log("logged in")
    session = api.create_session(args.session_name)
    log("created session %d passcode %s" % (session["id"], session.get("passcode")))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, _raise_interrupt)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    pods, rows, rc = [], [], 0
    host_sampler = HostSampler(AUDIO_LOG)
    t_start = time.monotonic()
    try:
        with open(args.out, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for step, target, new, t0, t1 in plan:
                for n in range(len(pods) + 1, target + 1):
                    pod = Pod(n, api, session, pcm, offsets[n - 1], ws_url, stop)
                    pod.join()
                    pod.start()
                    pods.append(pod)
                log("step %d: %d pods streaming" % (step, len(pods)))
                step_t0 = time.monotonic()
                while time.monotonic() - step_t0 < args.step_seconds:
                    stop.wait(POLL_SECONDS)
                    host = host_sampler.sample()
                    now = time.monotonic()
                    base = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "elapsed_s": round(now - t_start, 1), "step": step, "pods_target": target,
                            "step_elapsed_s": round(now - step_t0, 1)}
                    for pod in pods:
                        row = dict(base, **pod.snapshot(), **host)
                        rows.append(row)
                        writer.writerow(row)
                    f.flush()
                    lags = [r["lag_s"] for r in rows[-len(pods):] if r["lag_s"] is not None]
                    log("step %d t=%.0fs pods=%d connected=%d p95 lag=%s max=%s transcripts=%d degraded=%d drops=%d load=%.1f gpu=%s%%"
                        % (step, now - step_t0, len(pods), sum(1 for p in pods if p.ws is not None),
                           core.percentile(lags, 95), max(lags) if lags else None,
                           sum(p.transcripts_total for p in pods), sum(1 for p in pods if p.asr_state == "degraded"),
                           host["drop_warnings"], host["load1"], host["gpu_util"]))
    except KeyboardInterrupt:
        log("interrupted")
    except Exception as e:  # a failed join/poll setup: tear down cleanly, report, exit 1
        log("aborted: %s" % e)
        rc = 1
    finally:
        stop.set()
        for pod in pods:
            pod.close()
        for pod in pods:
            for t in pod.threads:
                t.join(timeout=5)
        if args.keep:
            log("--keep: session %d left as is (end and delete it yourself)" % session["id"])
        else:
            for what, call in (("end", api.end_session), ("delete", api.delete_session)):
                try:
                    call(session["id"])
                    log("%s session %d: ok" % (what, session["id"]))
                except Exception as e:
                    log("%s session %d FAILED (%s) -- clean it up from the dashboard" % (what, session["id"], e))
        if rows:
            steps_summary, verdict = core.summarize(rows, args.max_lag, args.warmup_seconds)
            for s in steps_summary:
                log("step %(step)d: %(pods)d pods, %(ticks)d ticks, lag median=%(median_lag_s)s p95=%(p95_lag_s)s "
                    "max=%(max_lag_s)s worst-tick-p95=%(worst_tick_p95_s)s, degraded pods=%(degraded_pods)s, "
                    "drop warnings=%(drop_warnings)s, gpu util mean=%(gpu_util_mean)s max=%(gpu_util_max)s, "
                    "gpu mem max=%(gpu_mem_mb_max)s MB, load1 max=%(load1_max)s, passed=%(passed)s" % s)
            log(verdict["text"])
            summary_path = os.path.join(os.path.dirname(os.path.abspath(args.out)), "summary.json")
            with open(summary_path, "w") as f:
                json.dump({"session_id": session["id"], "session_name": args.session_name, "base_url": args.base_url,
                           "audio": args.audio, "csv": os.path.abspath(args.out), "steps": steps_summary,
                           "verdict": verdict}, f, indent=2)
            log("summary -> %s" % summary_path)
    return rc


if __name__ == "__main__":
    sys.exit(main())
