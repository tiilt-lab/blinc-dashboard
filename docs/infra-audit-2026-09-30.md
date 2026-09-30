# Infrastructure and robustness audit — 2026-09-30

**Question asked:** will nublinc.com stay up and smooth with many simultaneous connections (classrooms × pods × tabs) while the GPU/CPU run analyses, and what can an adversary do to it?

**Method.** Seven parallel read-only code passes (API serving stack, live audio/video pipeline, post-hoc pipeline, security/abuse, failure modes and recovery, database, browser client), then host-level verification on glamdring-vm-1 (nginx, systemd, kernel, GPU, MySQL, Redis, disk, logs) and spot-checks of every high-severity claim against the code and the live schema. Nothing was modified. Tags: **CONFIRMED** = read end-to-end in code or checked on the host/DB; **LIKELY** = inferred. Where two passes disagreed, the live host decided (see "Corrections").

## STATUS — Phase 0 applied 2026-09-30 (same day)

Applied and verified on the host, no code beyond commit `1381557`:

- nginx: `worker_processes auto` (8 workers), `worker_rlimit_nofile 65535`, `worker_connections 8192`; `client_max_body_size 10m` server-wide with 6g on `/api/v1/sessions/upload_video` (unbuffered) and 500m on `/api/v1/uploads/`; 600 s timeouts on `/api/v1/llmqueries/`, the per-device `summary`/`video` routes and exports; CSP/HSTS/X-Frame-Options/nosniff now reach `index.html` and `/assets/` (policy text unchanged, `'unsafe-eval'` kept). Backup of the previous files in `/var/backups/nginx-pre-phase0-*`.
- systemd drop-ins (`/etc/systemd/system/blinc-*.service.d/override.conf`): `LimitNOFILE=65536`, `Wants=network-online.target`, `TZ=UTC` on all five; `OMP_NUM_THREADS=2` on the four processors; `OOMScoreAdjust=-500` on the live units; `Nice=10 CPUWeight=20 IOWeight=20 OOMScoreAdjust=500` on the post-hoc units; `TimeoutStopSec=300` on the audio processor. All five restarted; effective soft nofile is 65536.
- ufw rule 1: deny TCP 5000, 5001, 9010, 9013, 9014, 9015 in on `enp1s0` (loopback and the tailnet unaffected; nginx verified still proxying).
- MySQL `innodb_buffer_pool_size` 128 MB → 1 GB via `SET PERSIST` (online). Redis `appendonly yes` written to redis.conf. 15 NVIDIA 595 packages on `apt-mark hold`. GPU persistence mode on.
- Logs: `/etc/logrotate.d/blinc` (daily, 7 kept, 500 MB max, copytruncate); the 4 GB post-hoc log compressed to 24 MB and truncated; journald capped at 1 GB (`/etc/systemd/journald.conf.d/50-blinc.conf`), now 992 MB.
- Backups: `blinc-db-backup.timer` dumps `discussion_capture_vlj` nightly at 08:30 UTC to `/var/backups/blinc-db/`, 14 days kept; first dump taken.
- Disk: 177 write-only post-hoc WAVs (7.5 GB) deleted from `src/video_processing/videorecordings`; 84 % used. Nothing is old enough to archive: the recordings are 20 GB from July and 44 GB from August 2026.
- Code (commit `1381557`): `SQLALCHEMY_ENGINE_OPTIONS` pool 20 + 30, pre-ping, 10 s wait, 1800 s recycle; `POST /api/v1/sessions` returns 507 when the disk is under 10 % free.

Not done, needs a decision: the co-tenants. `llama-server` (26.5 GB VRAM, 10 GB swap, started by hand from `~/src/llama.cpp-pr27210`) and `mongod` (8.4 GB RSS) are why BLINC processes sit in swap. Phases 1 and 2 are untouched.

## STATUS — Phase 1 applied 2026-09-30 (same day, commits ce0cd21..3438b59)

Live on nublinc.com after migration f8ae4e72c79c, a restart of all five units and a frontend rebuild:

- 7a. Post-hoc websockets require a per-pod ticket from `POST /api/v1/sessions/<sid>/devices/<did>/posthoc_ticket` (session write access; Redis `posthoc_ticket:*`, 15 min); both services bind 127.0.0.1. Verified: unticketed and bogus-ticket messages are refused through nginx.
- 7b. Biometric enrollment requires a token minted by `addstudent` (Redis `enroll_token:*`, 30 min, bound to the alias) on both the live audio and post-hoc video sockets.
- 7c. NOT changed (product decision): the anonymous student-read routes are rate-limited per IP only.
- 8. Video key check keeps pods on API errors (closes only on explicit 4xx, or after 10 min of continuous failure); BYOD reconnects with jitter for 3 min keeping mic/camera alive and replaying up to ~3 min of offline audio; video socket reconnects; `/me` denies only on 401/403.
- 9. Transcript / metric / connect / disconnect callbacks retry (1 s -> 60 s, 10 min) with `X-Idempotency-Key`; ingest is idempotent on natural keys and batched.
- 10. ASR ingest is non-blocking with absolute-sample timestamps and drop counters; CrisperWhisper spawns go through a 30-300 s breaker with a 3.5 GB free-VRAM check; worker stderr in src/audio_processing/crisper_worker.stderr.log; pods receive `asr_status`; optional stages no longer drop transcripts.
- 11. `?after_id=` on the three polled routes (+ authenticated twin); `join_room` delta replay; websocket-first Socket.IO; chained/paused polling.
- 12. Indexes: UNIQUE session_device.processing_key, UNIQUE user.email, transcript.speaker_id, session.passcode, session(owner_id, creation_date); EXISTS rewrite for the admin sessions list.
- Also: video frame batches bounded to 2 per pod (oldest dropped, counted); autoPing 10/20 s on both live factories; recording-filename parsing fixed for post-hoc callbacks.

Still open from the audit: Phase 2 (WSGI server, GPU lease, post-hoc correctness/cancel, migration reconciliation), the resolution/fps follow-up at the video ffmpeg spawn, fragment-aware post-hoc recording lookup (172 pods have fragments), `VideoProcessor.stop()` join on the reactor, co-tenants.

## STATUS — Phase 2 and the scaling items applied 2026-09-30 (commits 0327ba4..871492a)

Live on nublinc.com after migrations 1f2e3d4c5b6a, 2a3b4c5d6e7f, 3b4c5d6e7f80, new units and a restart of everything:

- 13. API serves under gunicorn: 3 gthread workers x 64 threads (`DC_API_WORKERS`), `/etc/systemd/.../blinc-discussion-capture.service.d/override.conf` overrides ExecStart. A new `blinc-coordinator.service` runs the device websocket server (9011), the scheduler, the post-hoc queue runner and the boot tasks; device commands travel over Redis pub/sub; watchers, connect times, LLM slots, remux/synthesis locks and the image queue are Redis-backed. Socket.IO is websocket-only on both ends (open browser tabs from before the deploy must reload once).
- 14. Live presence keys (`live_pod:*`) gate post-hoc work; single-slot GPU lease (`gpu_lease:0`, 3 min TTL, heartbeat); a running post-hoc job is pre-empted when a class starts and re-queued; speaker embeddings on CUDA when >= 1.5 GB free; per-segment work through a 4-thread pool; batched ECAPA windows.
- 15. Post-hoc results are staged and replace the old ones only on success; `posthoc_failed` callback; worker process groups killed on cancel/pre-emption/timeout; claims store pid:boot-id with heartbeats and stale cleanup; recordings split by reconnects are joined in order; status-probe connection leak fixed.
- 16. Migration chain rebuilds production exactly; ON DELETE rules on the session/user/folder graphs; delete_* are single statements; account tokens purged after 30 days; CI job builds the chain against MySQL 8.
- Scaling 1-2. Silero VAD skips silent ASR windows (`DC_ASR_VAD`), a pool of 2 lazily spawned CrisperWhisper workers (`DC_ASR_WORKERS`), batch-capable worker protocol (no real batch API in crisperwhisper 2.0.3), `DC_ASR_MODEL` knob.
- Scaling 3. Metrics on loopback from every service (API /metrics, 9110-9115), Prometheus/Grafana/Alertmanager via `deploy/monitoring/docker-compose.yml` (127.0.0.1:9090 / :3000 / :9093; Grafana admin password in deploy/monitoring/.env), NVML GPU exporter unit, 15 alert rules with a webhook receiver still to be pointed somewhere.
- Scaling 4. Per-session `live_video_analytics` (create dialog checkbox; off = record only, video post-hoc leg queued at session end); live video decode on NVDEC with software fallback; video exporter on 9112.
- Tooling: `tools/loadgen` (see README) with a dedicated `loadgen@nublinc.com` teacher account (credentials in ~/.config/blinc-loadgen.env).

Not done: anonymous student-read routes (product decision; rate-limited), co-tenants (parked), video post-hoc results are still wiped at run start, multi-host split.

Earlier reports this builds on: `server-load-report-2026-08-06.md` (disk and ffmpeg-fan-out warnings, still open) and `docs/codebase-sweep.md` (P1/P2 antipatterns, fixed 2026-08-09).

---

## Executive summary — what breaks first

| # | Finding | Where it bites |
|---|---|---|
| 1 | **nginx runs 1 worker × 512 connections; each proxied socket costs 2 → ~250 concurrent sockets for the whole site** (CONFIRMED, `/etc/nginx/nginx.conf` has `events {}` and no `worker_processes`). | The first class where pods + dashboards + socket.io exceed ~250 sockets: new connections hang. |
| 2 | **The API is Werkzeug's dev server** (`discussion_capture.py:87 allow_unsafe_werkzeug=True`, `app.py:77 async_mode='threading'`), one process, unbounded threads, one GIL, and **every BLINC service runs with a soft file-descriptor limit of 1024** (CONFIRMED from `/proc/<pid>/limits`). | ~1000 sockets+files per process → EMFILE; long-polls and websockets each pin a thread. |
| 3 | **Anonymous post-hoc websockets can wipe a pod's analysis and monopolise the GPU; live pod sockets accept enrollment media without a key; both bind 0.0.0.0 and ufw allows every port from campus ranges** (CONFIRMED). | Any campus user, or anyone via the nginx `wss://` paths, can delete transcripts by id, cancel runs, or load 7–10 GB of models onto the GPU at will. |
| 4 | **An API restart (~6.5 s) closes every video-streaming pod; an audio-processor restart (~70 s) ends every BYOD pod** because the video key check treats any error as "revoked" and the browser retries once after 2 s (CONFIRMED). | Every deploy mid-class = students see "session closed". |
| 5 | **Transcripts and speaker metrics are POSTed to the API once, never retried** (CONFIRMED, `audio_processing/callbacks.py:43-49`). | Any API blip or MySQL restart silently loses utterances for good. |
| 6 | **ASR ingest queue is 3 chunks (~0.77 s), `put(timeout=0.05)` runs on the reactor, and dropped audio shifts every later timestamp** (CONFIRMED). One serialised CrisperWhisper worker caps throughput at ~10–12 talking pods; it crash-loops with stderr discarded (`crisperwhisper_asr.py:123`). | Under load all pods stall 50 ms per full chunk, transcripts drift early, and outages leave "connected pod, no transcript, no error". |
| 7 | **No admission control for GPU or CPU.** Post-hoc jobs load models from scratch (7–10 GB each), run concurrently with live classes and with each other, the UI "Run" button bypasses the queue, one thread is spawned per utterance/segment with 8 torch threads each, and a co-tenant `llama-server` currently holds 26.5 GB VRAM and 10 GB of swap (CONFIRMED on host). | CUDA OOM mid-class (this already happened on 09-23/24). |
| 8 | **Every dashboard join/reconnect replays the whole session** (`routes/socket.py:44-73`; session 414 = 36,944 video-metric rows ≈ 5 MB) and **student pages re-download full history every 2 s** (`byod-join-component.jsx:645`). Teacher tab + N pods ≈ 15.5 + 60·N requests/min, each growing with session length (CONFIRMED). | O(n²) DB and JSON work; an API restart makes every tab replay at once. |
| 9 | **Database:** default pool (5 + 10, no `pool_pre_ping`), connections held across ffmpeg/LLM work, per-row commits from ingest, no index on `session_device.processing_key`, no UNIQUE on `user.email`, several FKs without cascade (deletes 500), and a migration chain that cannot rebuild the live schema (CONFIRMED against `information_schema`). | Pool timeouts at ~15 concurrent slow requests; delete failures; unreproducible schema. |
| 10 | **Disk and logs:** 68 GB of video recordings in the working tree incl. 7.8 GB of WAVs post-hoc writes and never reads, 22.7 GB audio WAVs, a 4.0 GB unrotated log growing 125 lines/10 s, disk at 85 %, no automated DB backup, no free-space check before a session (CONFIRMED). | Disk-full is the failure the August report predicted and it fails silently. |
| 11 | **Memory:** 12.8 GB of 16 GB swap in use; the live audio processor has 700 MB swapped out; `mongod` (unrelated tenant) holds 8.4 GB RSS; MySQL buffer pool is the 128 MB default at 87 % full (CONFIRMED). | Page-in stalls at session start; the 09-24 swap incident can recur. |
| 12 | **Client:** single reconnect attempt with no jitter, any non-200 on `/me` logs the teacher out, source maps are public (47 `.map` files), CSP/HSTS never reach the document (CONFIRMED). | Deploys bounce teachers to /login; clickjacking of "End session" is possible. |

---

## Host-level facts (verified directly on glamdring-vm-1)

- **nginx 1.30.5**, HTTP/1.1+2+3. `worker_processes` unset (=1 on 8 cores), `worker_connections` unset (=512), no `client_max_body_size` (=1 MB vs app `MAX_CONTENT_LENGTH` 6 GB; two 413s already in the access log), no per-location `proxy_read_timeout` for `/api` (=60 s; one 504 on `generate_llm_feedback_based_on_metrics`). Websocket upgrades do work through nginx (101s logged).
- **Bindings:** 5001 (API, plain HTTP), 5000, 9010, 9013, 9014, 9015, 8501, 8765, 8766, 8792 listen on **0.0.0.0**; 9011, MySQL, Redis, llama-server on 127.0.0.1. **ufw rules 6–38 allow every port from 10.0.0.0/8, 129.105.0.0/16, 165.124.0.0/16** (campus). So from campus wifi the API and pod sockets are reachable directly, without TLS and without nginx.
- **systemd (all five blinc units):** `Restart=always`, no `LimitNOFILE` (soft 1024), no `Nice`/`CPUWeight`/`IOWeight`/`MemoryMax`/`OOMScoreAdjust`, no `Wants=network-online.target`, no ordering against the NVIDIA driver, no `TZ`. `blinc-video-processor` has restarted 22 times.
- **Kernel:** `somaxconn` 4096 (fine), `rmem_max/wmem_max` 212 KB (low for QUIC), `tcp_keepalive_time` 7200 s, `swappiness` 60.
- **Memory:** 32 GB RAM, 16 GB swap with 12.8 GB used. Swap by process: llama-server 10.0 GB, blinc audio-processor 704 MB, video-processor 349 MB, audio-posthoc 333 MB.
- **GPU:** Quadro RTX 8000, 34.5/48 GB used at idle: llama-server 26.5 GB (started manually from `~/src/llama.cpp-pr27210`, not the disabled unit), audio-posthoc 5.5 GB, video-posthoc 1.5 GB, live audio 0.9 GB. Persistence mode off. NVIDIA packages are not held from unattended-upgrades.
- **MySQL 8:** `max_connections` 151 (max used 9), `innodb_buffer_pool_size` 128 MB (1024 of 8192 pages free), hit rate 99.96 % today, `Select_full_join` 83. Data ≈ 130 MB; biggest tables `speaker_video_metrics` 320k rows/46 MB, `speaker_transcript_metrics` 89k, `transcript` 41k. **One DB dump exists (made today); no scheduled backup, no cron, no timers.**
- **Redis:** localhost only, no password (acceptable), `appendonly no`, RDB snapshots only, `maxmemory` unset.
- **Disk:** 791/984 GB (85 %). `src/video_processing/videorecordings` 68 GB (510 files; 149 WAVs = 7.8 GB), `src/audio_processing/recordings` 22.7 GB (321 WAVs), `/var/lib/mongodb` 43 GB, `/var/log` 5.7 GB, journald 3.9 GB, `audio_posthoc_processing_service.log` 4.0 GB with no logrotate entry.
- **Recording fragments:** 172 pods have more than one audio recording file; post-hoc opens `files[0]` of an unsorted glob (`server_posthoc.py:376-379`).

---

## A. API serving stack

- **CRITICAL (CONFIRMED) Werkzeug + threading mode.** `socketio.run(..., allow_unsafe_werkzeug=True)`; each websocket ≈ 2 threads, each 25 s long-poll pins a thread, no request timeouts, all ORM/JSON work under one GIL alongside the Twisted device server and APScheduler. `eventlet` is installed in the venv; `gunicorn` is not. *Fix:* `LimitNOFILE=65536` now; then serve with gunicorn `gthread` (1 worker, ~200 threads) or eventlet. Multi-worker needs nginx `ip_hash` plus moving `watchers._last_watched`, `image_queue_dict` (`session.py:32`), `_remux_locks` and the device `ConnectionManager` out of process memory.
- **HIGH (CONFIRMED) Full-history replay on every join** (`routes/socket.py:44-73`; client clears state on `room_joined`, `active-session-service.js:114-118`). With `message_queue` set, every per-sid emit is also pickled through Redis though no other process consumes it. *Fix:* client sends last-seen id; server returns only newer rows; `ignore_queue=True` on per-sid emits or drop `message_queue` until there are multiple workers.
- **HIGH (CONFIRMED) DB pool defaults, connections held through long work.** No `SQLALCHEMY_ENGINE_OPTIONS` (`app.py:81`); the first query opens a transaction that lives until teardown, covering ffmpeg remux/transcode (`session.py:343-448`, 300 s), LLM calls (600 s) and upload ffmpeg (600 s). *Fix:* `pool_size=20, max_overflow=30, pool_pre_ping=True, pool_recycle=1800, pool_timeout=10`; `db.session.close()` before ffmpeg/LLM; run uploads' ffmpeg on the post-hoc queue (open item #18 from the August sweep).
- **MEDIUM (CONFIRMED) nginx `/api` has no timeouts or body cap** (`blinc-app-locations.conf:84-87`). *Fix:* 600 s `proxy_read_timeout` for LLM/video/upload locations; `client_max_body_size 6g` + `proxy_request_buffering off` on the upload route only; keep 1–10 MB elsewhere.
- **MEDIUM (CONFIRMED) Per-row commits on ingest** (`callback.py:317-320` → `database.py:239`; `add_keyword_usage` `database.py:361`). *Fix:* `add_all` + one commit per callback, as `add_speaker_hr_metrics_batch` already does; build JSON before commit or `expire_on_commit=False`.
- **LOW–MEDIUM (CONFIRMED)** `_pod_ids_with_recordings` lists 895 files per overview poll (every 8 s per tab); `/api/v1/health` shells out to `nvidia-smi` and `systemctl` on every 5 s poll; sessions list unpaginated and, for admins, joins the entire `speaker_video_metrics` table. *Fix:* cache 5 s, restrict health to admins, `EXISTS` subquery, paginate.

## B. Live audio/video pipeline

- **CRITICAL (CONFIRMED) Ingest queue blocks the reactor and drops audio.** `server.py:208,383` `put(timeout=0.05)` on the reactor into a `maxsize=3` queue (`:407`); the consumer stops draining while `_flush` waits on the shared worker lock (`crisperwhisper_asr.py:229-245`); `_window_start` advances only by kept audio (`:237,:258`) while `AudioBuffer` keeps everything, so attribution extracts the wrong audio. *Fix:* `put_nowait`, consumer only appends and hands finished windows to a second queue, timestamps from absolute sample count, WARNING-level drop counter.
- **CRITICAL (CONFIRMED) One serialised worker, no backoff, no stderr.** Single `threading.Lock` (`:174`) for all pods (~1 s GPU per 12 s window ⇒ ~10–12 pods); spawn stderr=DEVNULL (`:123`); 180 s start + 120 s request timeouts held under the lock (up to ~7 min with no ASR for anyone); immediate respawn (51 "died/failed" lines on 09-23/24, cause unrecorded). *Fix:* stderr to a rotating log; circuit breaker with 30–300 s backoff and a `mem_get_info` check; `asr_degraded` status to the dashboard; FIFO queue with batched inference or a second worker.
- **HIGH (CONFIRMED) Video memory unbounded.** Frames decoded at native resolution (`video_processing/server.py:216`), 40-frame batches × 50 queued per pod (`videoprocessor.py:346`) ≈ 12 GB per lagging 1080p pod; one GPU detection thread round-robins all pods. *Fix:* `-vf fps=10,scale=-2:720 -threads 2`; per-pod queue of 1–2 keeping only the latest.
- **HIGH (CONFIRMED) CPU fan-out uncapped.** Thread per ASR segment (`processor.py:185`); ECAPA on CPU (`server.py:51`, no `run_opts`); ~30 encodes per segment (`segment_split.py:62-84`); no `OMP_NUM_THREADS`/`torch.set_num_threads` anywhere, on 8 cores shared with ffmpeg, video models, post-hoc and llama-server. *Fix:* bounded executor (4), batch the windows into one `encode_batch`, ECAPA on CUDA, `Environment=OMP_NUM_THREADS=2` in units.
- **HIGH (CONFIRMED) One exception loses the transcript.** `processor.py:195-345` is one `try`; keywords/features/topic/DoA and the transcript POST share it, so a CUDA OOM in keyword embedding drops the utterance. `speaker_metrics.py:151-188` holds `process_lock` across a 30 s HTTP call. *Fix:* guard each optional stage, always post the transcript, HTTP outside the lock.
- **MEDIUM (CONFIRMED) No websocket keepalive or payload cap.** `WebSocketServerFactory()` without `setProtocolOptions` (audio `:486`, video `:802`, post-hoc too). Dead peers persist up to 300 s holding ASR threads and ffmpeg. *Fix:* `autoPingInterval=10, autoPingTimeout=20, maxMessagePayloadSize`.
- **MEDIUM (CONFIRMED) `VideoProcessor.stop()` joins for up to 10 s on the reactor** (`videoprocessor.py:131-139` via `server.py:736`): end of class = N × 10 s freeze. *Fix:* `defer_blocking`.
- **No limit on concurrent pods or sessions anywhere (CONFIRMED).** *Fix:* admission control, e.g. record-only above N.

## C. Post-hoc pipeline

- **CRITICAL (CONFIRMED) No global job or VRAM limit.** Only same-pod duplicates are refused (`server_posthoc.py:163`, video `:205`); UI "Run" starts over the websocket and skips the queue (`posthoc-trigger.jsx:448,461`); queue runs audio and video simultaneously (`posthoc_queue.py:120-127`); timeouts move on without cancelling (`:159`); every job loads Qwen/WhisperX/Crisper (5–8 GB) + pyannote (2 GB) from scratch. *Fix:* Redis GPU-slot semaphore with owner token + TTL taken in `signal_start` before model load; free-VRAM check; route UI reruns through `enqueue`; defer while a live session is active.
- **HIGH (CONFIRMED) Thread per utterance** (`processor_posthoc.py:250-253`); batch ASR emits all segments at once (`qwen3_asr.py:93-103`) ⇒ hundreds of threads × ECAPA on CPU × blocking POSTs into the threaded API. Units have no `Nice`/`CPUWeight`. *Fix:* pool of 2–4, `torch.set_num_threads(2)`, batched POSTs, `Nice=10 CPUWeight=20 IOWeight=20`.
- **HIGH (CONFIRMED) Old results wiped before new ones exist; failures look like success.** `post_posthoc_reset` (`server_posthoc.py:442`) runs before ASR; OOM is swallowed (`qwen3_asr.py:104-108`); POSTs tried once (`callbacks.py:43-49`); an API restart re-queues the running job which then errors as "already running" while the original keeps going. *Fix:* staging run swapped in on success, `posthoc_failed` callback, retries, posted-vs-emitted count check.
- **MEDIUM (CONFIRMED) Stale claims** last 3 h (`pod_registry.py:22`), not cleared on restart (`callback.py:113-125`); claim value `"1"` released by unconditional DEL (`distributed_claim.py:60,73`), never renewed. The queue restarts the audio-posthoc unit via pinned sudo above 14 GiB RSS assuming idleness (`posthoc_queue.py:183-195`) and can kill a UI-started run. *Fix:* clear prefix on start; pid/boot-id in claim; compare-and-delete; heartbeat; check running jobs before recycling.
- **MEDIUM (CONFIRMED) Cancel/timeout don't free the GPU.** `cancel_posthoc` releases the claim (`:325-336`) but `stop` only sets a flag (`base_asr.py:172`); the worker runs up to 2 h and still posts results; stopping the queue from one session clears every session's jobs (`session.py:1075,1083`). *Fix:* `Popen` + kill process group; release claim after child exit; scope stop to the session.
- **MEDIUM (CONFIRMED) Connection leak + dead cleanup.** Audio only removes connections with a config (`:501`), every status probe leaks one, the sweep logs each every 10 s (the 4 GB log); video protocol lacks `send_close` so its sweep crashes on first stale connection and the idle memory-reclaim restart has fired 0 times since 08-08.
- **MEDIUM (CONFIRMED)** ffmpeg audio recovery inline on the event loop with 600 s timeout (`:465`); whole `.dat` read into RAM (`:183`); ~1.2 GB RAM per 2 h recording per job (`audio_stream_reader.py:51-57`); every video run writes a full-length WAV nothing reads (`videoprocessor_posthoc.py:149`).
- **LOW (LIKELY)** vLLM path would reserve `gpu_memory_utilization=0.5` (~24 GB) if ever enabled (`qwen3_worker.py:33`); not active today.

## D. Security and abuse (adversarial)

Context: `/api/v1/register` is open (rate-limited 15/min, no invite), so "teacher" findings are reachable by anyone. Transcripts are recordings of (often minor) students, so every anonymous read is PII exposure.

- **CRITICAL (CONFIRMED) Unauthenticated post-hoc websockets.** `common/ws_protocol.py` has no auth; `server_posthoc.py:109` (audio) / `:198` (video) accept a raw `sessiondeviceid`; `signal_start` → `post_posthoc_reset` → `delete_pod_analysis` (`callback.py:98`) wipes prior transcripts and hand edits; `cancel_posthoc` cancels any run. Exposed via `wss://nublinc.com/audio_posthoc_socket` and directly on 0.0.0.0:9014/9015 from campus. *Fix:* require the pod `processing_key` (as live `start` does at `processing_config.py:62`) or a short-lived HMAC ticket minted after `verify_session_access`; bind 127.0.0.1.
- **CRITICAL (CONFIRMED) Live sockets accept enrollment media with no key.** `save-audio-video-fingerprinting` (`server.py:114`) and binary blobs are processed (moviepy/ffmpeg/ECAPA) before any key check; a failed quality gate deletes the existing `.wav`/`.emb.npy`. Anyone can replace or delete any student's voice/face print or burn CPU/GPU. *Fix:* single-use enrollment token from `addstudent`, tied to the alias; verify before decoding media.
- **CRITICAL (CONFIRMED, "open by design")** Mass anonymous read by sequential id: `/api/v1/devices/<id>/transcripts/client` (`session.py:716`, no decorator), `/session/<sid>/.../transcripts/student/<alias>` (`:747-748`), `/students/<username>/longitudinal` (`:1102`), `/sessions/student/{passcode,sessionid,alias}` (`:92-108`), `/devices/<id>/session_device` (`:1113`, limited fields). The code comment says any gate needs a credential the account-less student-dashboard and expert-rating flows can hold. *Fix:* those flows already hold the session passcode or pod key; derive a per-session bearer from it and gate on that.
- **MEDIUM (CONFIRMED)** `MAX_CONTENT_LENGTH` 6 GB applies to every route (`app.py:58`) with no nginx cap ⇒ any authed POST can stream gigabytes. Invite spam: any editor can create accounts for arbitrary addresses and send BLINC-branded mail (`folder.py:160`, 60/h). LLM endpoints `generate_llm_feedback_based_on_metrics` (`llm_query.py:160`) and `fetch_response_for_question` (`:215`) are anonymous, bounded only by a 3-slot semaphore, hold threads 600 s and burn Gemini quota. Upload and rerun endpoints have no per-user quota.
- **LOW (CONFIRMED)** CSP `report-uri /api/v1/csp-report` has no handler; CSP/HSTS/X-Frame-Options are cancelled on `index.html` by the location's own `add_header` (enforcing the existing CSP would break nothing found; `'unsafe-eval'` and `img-src http://127.0.0.1` can go); login lockout is per (email, ip) so one IP can spray many usernames; 47 source maps served publicly.
- **Done right:** `authz.py` fuses resolve+authorize; `ProxyFix` with one trusted hop so `verify_local` and rate buckets use the real client IP; secrets gitignored and root-only in `/etc/blinc/secrets.env` (`DC_SECRET_KEY` present); cookies HttpOnly+Secure+SameSite=Strict; pbkdf2 passwords; hashed API tokens with `compare_digest`; SHA-256-only reset tokens; enumeration-safe forgot-password; `safe_name`/`secure_filename`/`int()` on every client-derived path; bound-parameter raw SQL; admin cannot escalate to super.

## E. Failure modes and recovery

- **CRITICAL (CONFIRMED) API restart ends video pods.** Video key check goes over HTTP to the API (`video_processing/processing_config.py:67` → `callbacks.py:23-37`); any exception or non-200 returns `None`; the 5 s sweep closes the pod as "access revoked"; the client treats that as "session closed by the owner" (`byod-join-component.jsx:1821-1826`). Restart ≈ 6.5 s > 5 s sweep. The audio side keeps connections on error; video should too. *Fix:* close only on explicit 400; read Redis directly.
- **HIGH (CONFIRMED) Audio-processor restart ends BYOD pods.** 67–73 s downtime (models load at import, `server.py:49-51`); client retries once after 2 s with a fresh socket whose `opened` is false (`:1763-1784`); video `onclose` is empty (`:1846`) so video is silently lost for the rest of the session; each reconnect starts a new recording fragment and post-hoc analyses only `files[0]` (172 pods already fragmented). *Fix:* backoff for ~3 min, reconnect video, post-hoc joins sorted `_orig` fragments.
- **HIGH (CONFIRMED) No retry on transcript/metric/connect callbacks** (`audio_processing/callbacks.py:43-49`, `callbacks_common.py:35-58`). *Fix:* retry queue with backoff + idempotency key (source, start_time).
- **MEDIUM (CONFIRMED) Redis is a hard dependency:** limiter fails closed (`app.py:72`), `RedisLogin` too (`auth.py:36`), socket.io message queue drops emits incl. `room_joined`, `create_session` commits the DB row before the Redis write (orphans), pod keys are never rebuilt from the DB after a Redis restart (LIKELY: revokes all live pods), `appendonly no`. *Fix:* limiter in-memory fallback, rebuild keys for active sessions on start, `appendonly yes`.
- **MEDIUM (CONFIRMED)** No `pool_pre_ping` ⇒ first request after a MySQL restart fails. Stale post-hoc claims survive restarts (see C). Recorders swallow per-chunk write errors (`recorder.py:17-23`) so a full disk is invisible to teachers.
- **LOW (CONFIRMED)** `After=network-online.target` without `Wants=`; no ordering against NVIDIA (post-hoc engines silently fall back to CPU, `whisperx_asr.py:124`); API crash-loops at 1 s until MySQL is up; no SIGTERM handlers (default 90 s stop can SIGKILL a `.dat`→wav conversion); two spots use local time and only work because the host is UTC (`session.py:928-930`, `server.py:183`).
- **Already resilient:** disconnect callback retries 8× with backoff; all callbacks have 30 s timeouts off the network thread; browser socket.io auto-rejoins and de-dupes by id; audio key sweep tolerates Redis errors; post-hoc queue persisted to disk; GPU worker dies with its parent (`KillMode=control-group`, stdin EOF).

## F. Database and data integrity

- **HIGH (CONFIRMED)** Live views download each pod's full history every 2 s (routes `session.py:734-745`); the session-wide metrics POST (`:768`) passes an `IN (...)` of up to ~36k ids, past `range_optimizer_max_mem_size` ⇒ full scan. *Fix:* `?after_id=` incremental fetch; JOIN instead of IN.
- **HIGH (CONFIRMED)** Migrations cannot rebuild production: `session_device.posthoc_analyzed_date` exists live but no migration adds it; `unique_session_name` has none; `7da13891981c` re-creates `keyword_usage` (fails on empty DB); `a41421e7eae0` adds CASCADE to `transcript_ibfk_1` and `55b46b0c86b7` drops it (live: **NO ACTION**); eight migrated tables have no models and `env.py` has no `include_object`. *Fix:* dump live schema, one reconciling migration + `alembic stamp`, `include_object`, CI job running `upgrade head` on an empty MySQL.
- **MEDIUM (CONFIRMED live)** FKs without delete rules: `transcript.session_device_id`, `session_device.session_id`, `keyword_usage.transcript_id`, `keyword.session_id`, `session.topic_model_id`, `topic_model.owner_id`, `session.folder`, `folder.parent`, `session.owner_id`. `delete_topic_model` and `delete_user` fail with 1451 when a model is in use; `delete_session`'s manual cascade with `synchronize_session='fetch'` races with post-hoc inserts and the route swallows the error (`session.py:152`). *Fix:* CASCADE/SET NULL per relationship; `delete_session` becomes one DELETE.
- **MEDIUM (CONFIRMED live)** Missing indexes/constraints: no index on `session_device.processing_key` (looked up on every pod request and callback), no UNIQUE on `user.email` (check-then-insert race) or `student.username`, no index on `session.passcode` or `transcript.speaker_id` (`get_transcripts(speaker_id=)` scans the table). *Fix:* `CREATE UNIQUE INDEX ux_sd_processing_key ON session_device(processing_key)`, `ux_user_email`, `ix_transcript_speaker_id`, `ix_session_passcode`, `ix_session_owner_created`.
- **LOW** `speaker_video_metrics.student_username` VARCHAR(20) vs 64-char aliases (1406 in strict mode); `get_all_metrics_by_session` (`database.py:305`) joins video rows without a device constraint so exports mix pods; `account_token` never purged; metric tables grow without archiving; `app.py:29` file handler has no rotation.
- **Done well:** constant-query sessions list; composite indexes `idx_transcript_device_start` and `idx_svm_device_time` exist; new tables (`folder_member`, `account_token`, `speaker_hr_metrics`) have proper cascades and uniques; naive-UTC datetimes throughout; processors write only through HTTP callbacks; short `app_context` blocks off the reactor.

## G. Browser client

- **HIGH (CONFIRMED)** BYOD reconnect is a single 2 s attempt with no jitter; a reconnect restarts getUserMedia/AudioContext with `start_time: 0` and re-uploads fingerprints (`byod-join-component.jsx:1763-1791, 498-503`). *Fix:* "ever opened" ref, exponential backoff with full jitter 1→30 s, keep the stream alive, resume with offset.
- **HIGH (CONFIRMED)** Full-history polling every 2 s with no `since`, no in-flight guard, no pause when hidden (`:640-655`, `transcripts-component_client.jsx:17-30`, `student-dashboard-component.jsx:190`). Teacher overview polls triage/8 s, queue/15 s, devices/15 s regardless of N. *Fix:* `since=`, chained `setTimeout` 5–10 s, `visibilitychange`, back off on 5xx.
- **HIGH (CONFIRMED)** Any non-200 on `/me` = denied ⇒ a 502 during a deploy bounces the teacher to /login and tears down the socket (`protected-route.jsx:17-25`, `auth-service.js:219-237`). *Fix:* deny only on 401/403; otherwise keep `cachedUser` and retry.
- **MEDIUM (CONFIRMED)** Socket.IO connects polling-first (`socket-service.js:13`) on an out-of-date comment; ~4 polling requests per connection before upgrade (explains the 58:15 polling:websocket ratio; clients are not stuck). *Fix:* `transports:['websocket','polling']` or `rememberUpgrade:true`.
- **MEDIUM** No `bufferedAmount` backpressure on audio/video sends (`:501, :603-615`), empty video `onclose` (`:1846`), spinners hang on 5xx (join `:1641-1676`, sessions list), socket handlers are no-ops so a dead feed looks like a quiet class (`socket-service.js:20-28`), `initialize()` can leak a socket after unmount.
- **LOW** `new AudioContext({sampleRate:16000})` likely throws on Firefox with 48 kHz mics (`:1362`); HR batches spliced before POST are lost on failure (`:1042-1049`); main bundle eagerly imports SessionManager/SignupPage (socket.io, rxjs); `sourcemap: true` ships 47 maps; `localStorage.getItem` unguarded (`index.jsx:15`).
- **Done well:** single fetch wrapper with `cache: no-store`; pod key per tab in sessionStorage; digest de-dupe by id; 18 lazy routes; `vite:preloadError` reload; careful BYOD teardown; RxJS cleanup; 5 Mbps video cap.

---

## Corrections and disagreements resolved

- The database pass claimed transcripts have only the FK index so every poll filesorts. **Wrong:** `idx_transcript_device_start (session_device_id, start_time)` and `idx_svm_device_time (session_device_id, time_stamp)` exist on the live DB. The rest of that finding (full-history polling, huge IN lists) stands.
- The failure-mode pass could not read `/etc/blinc/secrets.env`; verified here that `DC_SECRET_KEY` is set, so API restarts do not invalidate sessions.
- The security pass reported the pod key appearing in URLs/logs; today's access log has 0 `?key=` requests, so treat it as a latent code path (`api-service.js:26-30`) rather than a current leak.
- The post-hoc pass said the queue "restarts the audio service" with sudo; sudoers pins it to `blinc-audio-posthoc-processor` only, so live audio is not at risk from it, but a UI-started post-hoc run is.

## Recommended order

**Phase 0 — config only, no code, one evening (each is a reload or unit edit):**
1. nginx: `worker_processes auto; worker_rlimit_nofile 65535; events { worker_connections 8192; }`; per-location timeouts and `client_max_body_size` for upload/LLM/video; re-include the security headers in the `index.html`/`assets` locations (drop `'unsafe-eval'`).
2. All five units: `LimitNOFILE=65536`, `Wants=network-online.target`, `Environment=TZ=UTC OMP_NUM_THREADS=2`; post-hoc units `Nice=10 CPUWeight=20 IOWeight=20`; live units `OOMScoreAdjust=-500`.
3. ufw: deny 5000/5001/9010/9013/9014/9015 from non-localhost (or bind them to 127.0.0.1; nginx is the only legitimate client). Bind 8501/8765/8766/8792 to localhost or Tailscale if they are not public.
4. `SQLALCHEMY_ENGINE_OPTIONS` (pool 20/30, pre-ping, recycle 1800, timeout 10); MySQL `innodb_buffer_pool_size=1G`; Redis `appendonly yes`; `apt-mark hold` the NVIDIA driver; `nvidia-smi -pm 1`.
5. Disk: delete the 7.8 GB orphan WAVs, archive recordings older than a term off the working tree, logrotate for `src/**/*.log`, `SystemMaxUse=1G` for journald, a nightly `mysqldump` timer to `/var/backups` (keep 14), a free-space check that refuses to start a session under 10 %.
6. Decide the co-tenants: llama-server (26.5 GB VRAM, 10 GB swap) and mongod (8.4 GB RSS) are the reason BLINC processes are in swap. Cap llama-server's context or run it only outside class hours.

**Phase 1 — small code changes, one to two days:**
7. Auth on post-hoc sockets and on enrollment (pod key / one-time ticket); bearer for the account-less student flows.
8. Video key check: keep pods on error, close only on explicit 400. BYOD reconnect with jitter and resume; video `onclose`; `/me` deny only on 401/403.
9. Retry queue for transcript/metric/connect callbacks with idempotency keys.
10. ASR ingest: `put_nowait`, absolute-sample timestamps, drop counter, worker stderr to a log, spawn backoff + free-VRAM check, `asr_degraded` status.
11. `since=` on the three 2 s polls; last-seen id on `join_room`; socket.io websocket-first.
12. Indexes: `processing_key` UNIQUE, `user.email` UNIQUE, `transcript.speaker_id`, `session.passcode`; batch ingest commits.

**Phase 2 — structural, a sprint each:**
13. Replace Werkzeug with gunicorn/eventlet (single worker first), then externalise in-process state for multi-worker.
14. GPU/CPU admission control: Redis GPU-slot lease shared by live and post-hoc, post-hoc deferred during live sessions, bounded thread pools, batched ECAPA on CUDA, resident post-hoc models.
15. Post-hoc correctness: staging results swapped on success, `posthoc_failed`, process-group kill on cancel, claim heartbeats, fragment-aware recording lookup.
16. Migration reconciliation + CI `upgrade head` on an empty DB; cascade rules; retention/archiving for metric tables.
