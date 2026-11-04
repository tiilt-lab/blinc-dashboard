# BLINC Discussion Capture

BLINC records small-group discussions and turns them into live and post-hoc
analytics. Each group ("pod") records audio, and optionally video, Polar
heart-rate straps and Muse EEG headbands, from a browser or a hardware
mic-array pod. The pipeline
identifies who is speaking from voice prints, transcribes the speech, and
computes discussion metrics (participation, cohesion, keywords, questions,
emotions, gaze and joint attention) that facilitators watch on a dashboard
while the discussion is happening. After the session a deeper pass
re-transcribes the recordings with speaker labels, and an utterance coder
codes every line against a coding scheme (the built-in one is the
negotiation scheme for the Kellogg "Viking" case).

## How a session flows

1. **Sign in and create a session.** A facilitator logs into the dashboard,
   creates a session and gets a word passcode (and QR code) to hand out. The
   session can be marked *paired* (two laptops per group, one per side of
   the table) and given a coding scheme for the post-session coding.

2. **Pods join.** A pod is one recording device covering one group:
   - **BYOD**: any laptop or tablet opens `/join`, enters the passcode and
     passes a device check. Groups are auto-named `Group A`, `Group B`, ... (a
     typed name overrides).
   - **Hardware pods**: ReSpeaker mic-array devices (udev rules in
     `deploy/respeaker.rules`) connect over the device websocket.

3. **Speakers enroll on the join screen.** Every speaker needs a voice print
   so the pipeline can attribute speech. A speaker card offers a quick
   recording (at least 10 s of natural speech), or typing an enrolled
   username attaches that student's saved print. When the card's name is a
   registered username that has no print yet, the quick recording is also
   kept as that student's voice print (the sign-up quality gate decides,
   with a lower speech floor; an existing print is never replaced from a
   pod). Full enrollment (a coached, longer voice sample plus face capture
   for video) happens ahead of time in the sign-up flow (`/signup`). Polar
   H10 straps and Muse headbands are paired per speaker over Web Bluetooth
   on the same screen; a headband's "sync timing" button (press, then tap
   the band twice) checks its timing against the pod's audio.
   Someone arriving late is added from the Options menu during the session.

4. **Live capture and processing.** On "start" the browser streams:
   - **Audio** over `/audio_socket` to the audio processor: voice-activity
     gating and windowing (`asr_ingest.py`), speaker identification (ECAPA
     embeddings matched against the pod's prints), transcription by
     CrisperWhisper 2.0 in a local worker pool (`asr_connectors/`), then
     per-utterance features: keyword matching (sentence embeddings), question
     detection, expression and thinking-style scoring (LIWC, open or LLM),
     participation and cohesion metrics, optional prosody and vocal emotion.
   - **Video** (optional) over `/video_socket` to the video processor: face
     recognition against enrolled faces, facial emotion, gaze and attention
     (Gaze-LLE), object of focus (YOLO), joint attention. Frames are decoded
     by ffmpeg, on the GPU when one is present (`DC_VIDEO_HWDEC`).
   - **Heart rate** from paired Polar straps and **EEG band power** from
     paired Muse headbands, posted to the REST API (`routes/wearables.py`).
     Samples are timed from each sensor's own counter, converted to the
     server clock in the browser (`server-clock.js`, `sample-clock.js`) and
     stored with millisecond session times, as are transcripts; sync taps
     found in the audio measure what is left (`sync_taps.py`). The Muse's
     raw 256 Hz EEG and 52 Hz accelerometer are kept too, losslessly, in
     their own table (`raw_signals.py`, `wearable_raw_block`), and download
     from the pod page as one CSV row per sample.

   The processors never write the database. They POST results to the API's
   `/api/v1/callback/*` routes, which persist to MySQL and push live updates
   to dashboards over Socket.IO. Redis carries session auth keys and
   configuration between the API and the processors, the live-presence and
   GPU-lease keys, the device-command bus and the post-hoc queue.

5. **Watch live.** The session overview shows every pod's transcript and
   metrics as they arrive, with session-level rollups, a devices tab, the
   discussion graph and a server-load tab.

6. **Post-hoc analysis.** When a session ends, the instructor queues a deeper
   pass from the pod page or the session overview. Audio: MOSS end to end
   (transcript and speaker labels in one pass, named from the enrolled voice
   prints; the default) or a CrisperWhisper verbatim re-run with voice-print
   matching. Video: head tracking with identity persistence, gaze overlays,
   configurable fps. The queue (`src/server/posthoc_queue.py`) runs one pod
   at a time, audio leg then video leg, yields to live classes (GPU lease and
   live-presence keys in `src/common/gpu_lease.py`) and survives browser
   refreshes.

7. **Utterance coding.** The last stage of the queue codes the post-hoc
   transcript utterance by utterance against the session's coding scheme
   (`src/server/negotiation_coding.py`, `coding_schemes.py`) using a local
   llama-server. Paired sessions merge both laptops' transcripts on the
   session clock first and drop the far side's copy of each line. Results
   roll up per team and per speaker, with a de-escalation timeline for the
   negotiation scheme, and export as CSV. Instructors write their own
   schemes in the Coding schemes library.

8. **Review and correct.** Transcript text can be edited (the ASR text is
   kept), utterances reassigned to the right speaker (optionally a whole
   diarization cluster at once), and session video plays back with gaze
   overlays. Account-less flows read a session's results by passcode and
   alias: the student dashboard (`/student/dashboard`) and the expert-rating
   page (`/expert/rating`).

## Architecture

Six systemd units, one virtualenv for all of them (`src/venv-unified`) plus
two helper venvs for the ASR engines, nginx, MySQL 8 and Redis. Everything
binds to loopback except the two live websocket services (see the sweep
notes in `docs/`); nginx is the only client.

| systemd unit                    | Entry point                                                              | Port env var (code default)                 |
| ------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------- |
| `blinc-discussion-capture`      | `gunicorn -c src/server/gunicorn.conf.py --chdir src/server wsgi:app`    | `DC_PORT` (5000), `DC_API_WORKERS` (3)      |
| `blinc-coordinator`             | `src/server/coordinator.py` (`DC_ROLE=coordinator`)                      | `DC_DEVICE_WS_PORT` (9001)                  |
| `blinc-audio-processor`         | `src/audio_processing/server.py`                                         | `DC_AUDIO_WS_PORT` (9000)                   |
| `blinc-video-processor`         | `src/video_processing/server.py`                                         | `DC_VIDEO_WS_PORT` (9003)                   |
| `blinc-audio-posthoc-processor` | `src/audio_processing/server_posthoc.py`                                 | `DC_AUDIO_POSTHOC_WS_PORT` (9005)           |
| `blinc-video-posthoc-processor` | `src/video_processing/server_posthoc.py`                                 | `DC_VIDEO_POSTHOC_WS_PORT` (9004)           |

The API runs as interchangeable gunicorn workers (`gthread`, 64 threads each,
websocket-only Socket.IO over a Redis message queue). Everything that must
exist exactly once lives in the coordinator: the Twisted device websocket
server, the session-timeout job, the post-hoc queue runner and the boot
tasks. API workers hand it device commands over Redis pub/sub. The
coordinator reads `DC_AUDIO_POSTHOC_WS_PORT` and `DC_VIDEO_POSTHOC_WS_PORT`
too, to reach the post-hoc services: set them on its unit whenever the
services run on non-default ports.

nginx serves the built frontend and proxies everything else, so the browser
only ever talks to one origin:

| nginx location          | Backend                       |
| ----------------------- | ----------------------------- |
| `/` `/assets/`          | `frontend/build/` (static)    |
| `/api`, `/socket.io`    | gunicorn (`DC_PORT`)          |
| `/device_socket`        | coordinator (device WS)       |
| `/audio_socket`         | audio processor               |
| `/video_socket`         | video processor               |
| `/audio_posthoc_socket` | audio post-hoc processor      |
| `/video_posthoc_socket` | video post-hoc processor      |

Two co-tenants the code talks to over HTTP: a **llama-server** at
`http://127.0.0.1:8080` for utterance coding (`NEGOTIATION_LLM_URL`,
`NEGOTIATION_LLM_MODEL`, default model `qwen3.8-27b-uncensored`,
`NEGOTIATION_LLM_PARALLEL` chunks in flight), and **Gemini** via
`GOOGLE_API_KEY` for the synthesized-feedback and question routes
(`routes/llm_query.py`) and the `scorer=llm` option. Invitation and
password-reset mail goes through MailerSend (`MAILERSEND_API_TOKEN`,
`MAIL_FROM_EMAIL`; mail is silently disabled without them).

Every process exports Prometheus metrics (`src/common/blinc_metrics.py`):
the API on `/metrics` under `DC_PORT`, the coordinator on 9110, live audio
9111, live video 9112, audio post-hoc 9113, video post-hoc 9114, the GPU
exporter 9115 (`BLINC_METRICS_PORT` overrides a unit's port, 0 disables it;
the live video service still reads `DC_VIDEO_METRICS_PORT` instead). The
Prometheus, Grafana and Alertmanager stack is in `deploy/monitoring/`.

Multiple instances share one host by giving each its own `DC_*` ports, MySQL
database, Redis db number and working directory. The production instance's
units and nginx block are in `deploy/instances/blinc-puthipiroj/` (ports
5001/9010/9011/9013/9014/9015) and are the templates to start from.

### Repository layout

```
frontend/              React + Vite app: dashboard, join page, sign-up, student and rater pages
src/server/            Flask API, Socket.IO, coordinator, MySQL models and Alembic migrations,
                       post-hoc queue, utterance coding and coding schemes
src/audio_processing/  live + post-hoc audio pipeline, ASR connectors (CrisperWhisper, MOSS),
                       speaker identification, features
src/video_processing/  live + post-hoc video pipeline (faces, emotion, gaze, heads); the model
                       code under yolo_head/, asd/, attention_tracking/, emotion_detector/models
                       and video_cartoonizer/model is vendored
src/common/            code shared by the services: metrics, GPU lease, Redis claims, clock and
                       recording helpers, websocket protocol, reactor-safety checks
tests/                 pytest suite (no DB, GPU or network) and manual e2e scripts
tools/                 loadgen (live-capacity test), coding_eval (coder accuracy), CI helpers
deploy/                nginx configs, udev rules, per-instance systemd units, dev containers,
                       monitoring stack
docs/                  audits, sweeps, model profiles, codebook notes
```

### Where data lives

The processors write beside their own code, not under a configurable root:

| What                                    | Path                                                    |
| --------------------------------------- | ------------------------------------------------------- |
| Live audio recordings                   | `src/audio_processing/recordings/`                      |
| Voice prints (`.npy`) and enrollment wav| `src/audio_processing/audiobiometrics/`                 |
| Enrollment takes (`.webm`)              | `src/audio_processing/audiovideobiometrics/`            |
| Video recordings and remuxed copies     | `src/video_processing/videorecordings/`, `videorecordings_fixed/` |
| Face embeddings and thumbnails          | `src/video_processing/facial_embeddings/`               |
| Topic models and their uploads          | `src/server/topicModels/`, `src/server/uploads/`        |
| Service logs                            | journald, plus a log file next to each service          |

`root_dir` in `src/server/config.ini` is only read by the admin student-merge
tool; nothing else uses it.

## Installing on a new machine

Target: Ubuntu 22.04 or newer with an NVIDIA GPU. CrisperWhisper is the only
live ASR and MOSS the default post-hoc engine, so a GPU is required for audio
as well as video. Development of the API and frontend needs no GPU (see
"Development").

### 1. System packages

```bash
sudo apt-get update
sudo apt-get install -y build-essential git curl pkg-config ffmpeg libsndfile1 \
    nginx mysql-server redis-server
sudo systemctl enable --now mysql redis-server
```

GPU: the recommended NVIDIA driver (`sudo ubuntu-drivers install`) plus CUDA
12.8 and cuDNN from NVIDIA's apt repositories. The pinned torch wheels are
cu128 builds, so a 570-series driver works and CUDA 13-only setups do not.

Python 3.13 for the unified venv (miniforge, deadsnakes or a system
python3.13) and Python 3.10 for the two engine venvs (CrisperWhisper in
`src/venv-crisper`, MOSS in `src/venv-granite`). Node 24 for the frontend
(`nvm install 24`).

### 2. Clone and create the Python environments

```bash
git clone https://github.com/tiilt-lab/blinc-dashboard.git chemistry-dashboard
cd chemistry-dashboard
python3.13 -m venv src/venv-unified
src/venv-unified/bin/pip install -r src/requirements-unified.txt --extra-index-url https://download.pytorch.org/whl/cu128
src/venv-unified/bin/pip install git+https://github.com/fkryan/gazelle
src/venv-unified/bin/python -c "import nltk; nltk.download('punkt'); nltk.download('punkt_tab')"

# CrisperWhisper worker (its CTranslate2 fork must not share a venv with the stack)
python3.10 -m venv src/venv-crisper
src/venv-crisper/bin/pip install -r src/requirements-crisper.txt
```

`src/requirements-unified.README.md` explains the hard pins (torch cu128
index, transformers 5.x, numpy 2) and how to rebuild the venv beside the
running one. `src/requirements-direct.txt` lists what the code imports
directly; CI's deptry job keeps the three files consistent.

The MOSS engine runs in a third venv, `src/venv-granite` (Python 3.10,
transformers 5.x, the OpenMOSS package from a checkout of
MOSS-Transcribe-Diarize, Triton). There is no lock file for it in the
repository yet; the "MOSS end to end" section below lists what goes in it.

### 3. Database

The MySQL password convention is *password = username*:

```sql
CREATE DATABASE discussion_capture;
CREATE USER 'blinc'@'localhost' IDENTIFIED BY 'blinc';
GRANT ALL PRIVILEGES ON discussion_capture.* TO 'blinc'@'localhost';
FLUSH PRIVILEGES;
```

Build the schema from the Alembic chain (either command works; the second
needs no `config.ini`):

```bash
(cd src/server && ../venv-unified/bin/flask --app discussion_capture db upgrade)
DC_DATABASE_URL=mysql+mysqlconnector://blinc:blinc@127.0.0.1:3306/discussion_capture \
    src/venv-unified/bin/python src/server/migrations/run_migrations.py upgrade
```

### 4. Configuration

Each component has a git-ignored `config.ini` created from its example:

```bash
cp src/server/config.ini.example           src/server/config.ini
cp src/audio_processing/config.ini.example src/audio_processing/config.ini
cp src/video_processing/config.ini.example src/video_processing/config.ini
```

Then edit:
- **`src/server/config.ini`**: `https`, `domain` (comma-separated list
  allowed; each becomes a Socket.IO CORS origin), `database_user`,
  `database_name`, optional `database_host`/`database_port`, `redis_db`,
  `proxy_count` (1 for nginx alone). Optional `[sync]` mirrors student
  profiles with a peer instance.
- **`src/audio_processing/config.ini`** and
  **`src/video_processing/config.ini`**: point the `[output]` callback URLs
  at your API port, set `redis_db` to **match the server's** (the server
  writes session auth keys the processors read). The audio file also picks
  `scorer=liwc|open|llm`, the keyword backend and the live sentence
  embedder; the video file picks the emotion, object, attention, face and
  head models and the post-hoc fps. In the video file set
  `cartoonize = false` (the example still has it on, and its checkpoint is
  not shipped, so the live video service would crash at start) and
  `processvideoanalytics` to taste; production records video live and
  analyses it post-hoc. `docs/model-profiles.md` lists the knobs.

Ports and runtime switches are environment variables on the units:

| Variable                                               | Default         | Meaning                                                                |
| ------------------------------------------------------ | --------------- | ---------------------------------------------------------------------- |
| `DC_PORT`, `DC_API_WORKERS`                            | 5000, 3         | gunicorn bind port and worker count                                     |
| `DC_DEVICE_WS_PORT`                                    | 9001            | device websocket (coordinator)                                          |
| `DC_AUDIO_WS_PORT`, `DC_VIDEO_WS_PORT`                 | 9000, 9003      | live processors                                                         |
| `DC_AUDIO_POSTHOC_WS_PORT`, `DC_VIDEO_POSTHOC_WS_PORT` | 9005, 9004      | post-hoc processors; set on the coordinator too                         |
| `DC_ROLE`                                              | `api`           | `api`, `coordinator` or `all` (the dev runner)                          |
| `DC_DISABLE_SCHEDULER`, `DC_SKIP_BOOT_TASKS`, `DC_DISABLE_POSTHOC_QUEUE` | unset | run a second coordinator beside production with those side effects off |
| `DC_DATABASE_NAME`, `DC_DATABASE_URL`                  | from config.ini | point one process at another database (migration checks)                |
| `DC_ASR_WORKERS`, `DC_ASR_BATCH`, `DC_ASR_MODEL`       | 2, 4, config    | CrisperWhisper worker pool                                              |
| `DC_ASR_VAD`                                           | silero          | speech gate: `off`, `energy` or `silero`                                |
| `DC_ASR_WINDOW_SECONDS`, `_CAP_SECONDS`, `_CARRY_SECONDS`, `_QUIET` | 10, 16, 1.0, 0.5 | live ASR window cutting                                        |
| `DC_VIDEO_HWDEC`                                       | on if `nvidia-smi` works | GPU decoding of incoming video                                 |
| `DC_POSTHOC_IDLE_RECYCLE_S`                            | 180             | a post-hoc service with no run exits (and restarts) after this long      |
| `DC_AUDIO_POSTHOC_MIN_FREE_MIB`, `DC_VIDEO_POSTHOC_MIN_FREE_MIB` | 8192, 14336 | free GPU memory a run needs before loading                        |
| `DC_POSTHOC_WORKERS`                                   | 4               | per-segment threads in the audio post-hoc pass                          |
| `BLINC_METRICS_PORT`                                   | per service     | Prometheus exporter port, 0 disables                                    |
| `NEGOTIATION_LLM_URL`, `NEGOTIATION_LLM_MODEL`, `NEGOTIATION_LLM_PARALLEL`, `NEGOTIATION_CODING_WAIT_FOR_IDLE` | see above | utterance coding                      |

### 5. Credentials

`/etc/blinc/secrets.env` (root-only, loaded by the API, coordinator and
post-hoc units through `EnvironmentFile`):

- `DC_SECRET_KEY`: the session-signing key. Without it the API generates a
  random one at start and every restart logs everyone out.
- `GOOGLE_API_KEY`: Gemini, for the feedback and question routes and
  `scorer=llm`. The API also reads a `.env` in `src/server/` (python-dotenv).
- `MAILERSEND_API_TOKEN`, `MAIL_FROM_EMAIL`, optional `MAIL_FROM_NAME`,
  `MAIL_REPLY_TO`, `MAIL_LINK_BASE`: invitation and reset mail.
- `HF_TOKEN` only if you configure a gated Hugging Face model; the defaults
  need none.

The default `liwc` scorer needs the proprietary LIWC2007 dictionary; use
`scorer=open` without a license. CrisperWhisper's weights are under a
non-commercial research license.

### 6. Model assets

Most models (ECAPA speaker ID, sentence embedders, CrisperWhisper, MOSS,
YOLO11, hsemotion, Gaze-LLE with its DINOv2 backbone) download themselves
from Hugging Face or torch.hub on first use; CrisperWhisper converts its
weights to CTranslate2 format once. Manual setup:

- **Head detector weights** in
  `src/video_processing/attention_tracking/pretrained-models/`: the vendored
  CrowdHuman `crowdhuman_yolov5m.pt` (default `head_model=yolov5`). The
  legacy checkpoints (`model_gazefollow.pt`, `yolov4-p7.pt`, the ResMasking
  emotion checkpoint, GoogleNews word2vec vectors) are only needed if you
  switch the corresponding `config.ini` backends away from their defaults.
- **llama-server** with the coding model, listening on 127.0.0.1:8080, for
  utterance coding (`NEGOTIATION_LLM_URL` to point elsewhere).

### 7. Frontend

```bash
cd frontend && npm ci && npm run build
```

nginx serves `frontend/build/` directly; rebuilding is deploying.

### 8. Services, nginx, TLS

Use the unit files in `deploy/instances/blinc-puthipiroj/` as templates:
edit `User`, `WorkingDirectory`, `ExecStart` paths and the `DC_*` port
overrides for your machine, then:

```bash
sudo cp blinc-*.service /lib/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now blinc-discussion-capture blinc-coordinator \
    blinc-audio-processor blinc-video-processor \
    blinc-audio-posthoc-processor blinc-video-posthoc-processor
```

Adapt `nginx-blinc.puthipiroj.com.conf` (plus `nginx-posthoc-locations.conf`
for the two post-hoc websockets, and `deploy/nginx-headers.conf` for the
security headers and CSP) to your hostname and ports, add the server block
to nginx's `http {}` context, then:

```bash
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d your.domain      # HTTPS is required for mic/camera capture
```

Optional: the monitoring stack (`deploy/monitoring/README.md`) and the GPU
exporter unit.

### 9. First user

```bash
cd src/server && ../venv-unified/bin/python create_user.py
```

Log in at your domain and create a session.

## Development

Most changes touch the frontend, the Flask routes or the analytics, and none
of that needs a GPU: the CI checks run on a plain laptop, and so does the API
server with the Vite dev server in front of it. Only the processors need the
CUDA machine.

### The checks CI runs

`.github/workflows/ci.yml` runs six jobs on every push and pull request:
the Python tests, ruff (zero violations on the F/E9/E722/B006/B023 rules),
deptry, the frontend (eslint with a ratcheted warning cap, `tsc`, vitest,
`vite build`), gitleaks over the branch's history, and the migration chain
round-tripped against a real MySQL 8. Locally, with Python 3.11 or newer and
Node 24:

```bash
python3 -m venv .venv-ci
.venv-ci/bin/pip install pytest ruff==0.16.2 deptry==0.25.1 numpy scipy flask==3.1.3 requests==2.34.2 prometheus_client==0.25.0 moviepy==2.2.1
.venv-ci/bin/python -m pytest tests/ -q        # unit + contract tests, no DB or GPU
.venv-ci/bin/ruff check src                    # must report no violations
bash tools/ci/deptry_check.sh                  # imports vs requirements-direct.txt vs the lock
cd frontend && npm ci && npm run lint && npm run typecheck && npm test && npm run build
```

The migration round trip needs the containers from the next section and an
empty database:

```bash
docker compose -f deploy/dev/docker-compose.yml exec mysql mysql -uroot -proot -e 'CREATE DATABASE IF NOT EXISTS blinc'
DC_DATABASE_URL=mysql+mysqlconnector://root:root@127.0.0.1:3306/blinc src/venv-server/bin/python src/server/migrations/run_migrations.py roundtrip
```

The e2e scripts in `tests/e2e/` drive a live deployment with real
credentials (Playwright) and are run by hand; see their headers.

### Running the site without a GPU

```bash
# 1. MySQL and Redis in Docker. The data lives in a named volume and
#    survives restarts; `down -v` wipes it.
docker compose -f deploy/dev/docker-compose.yml up -d

# 2. A server-only Python environment: Flask, the MySQL driver and what the
#    routes import. About 1 GB, no CUDA. Python 3.11 to 3.13.
python3 -m venv src/venv-server
src/venv-server/bin/pip install -r src/requirements-server.txt

# 3. Configuration. The example already matches the containers (user blinc,
#    database discussion_capture, localhost ports).
cp src/server/config.ini.example src/server/config.ini

# 4. Schema, then a first login (role 2 = admin).
(cd src/server && ../venv-server/bin/flask --app discussion_capture db upgrade)
(cd src/server && ../venv-server/bin/python create_user.py)

# 5. Run. Vite proxies /api and /socket.io to :5001 (frontend/vite.config.js).
DC_PORT=5001 src/venv-server/bin/python src/server/discussion_capture.py
cd frontend && npm ci && npm start              # http://localhost:3000
```

The dev runner (`discussion_capture.py`) is one process in the `all` role:
API, device websocket, scheduler and queue together. Everything the browser
asks the API for works: sign-in, sessions, People and Students, the
dashboard pages, every `/api` route. Nothing that needs a processor does:
live capture, voice enrollment, transcription, video, post-hoc runs, coding.
Gemini requests fail until a real `GOOGLE_API_KEY` is in the environment or
`src/server/.env`; the server starts without one.

If a MySQL or Redis already listens on this machine, start the containers on
other ports and point the server at them:

```bash
BLINC_MYSQL_PORT=3307 BLINC_REDIS_PORT=6380 docker compose -f deploy/dev/docker-compose.yml up -d
# in config.ini: database_port=3307 and redis_port=6380
DC_PORT=5091 DC_DEVICE_WS_PORT=9091 src/venv-server/bin/python src/server/discussion_capture.py
cd frontend && DC_API_TARGET=http://localhost:5091 npm start
```

### Processors by hand

On the GPU machine, run any processor from the unified venv the same way
(stop its systemd unit first):

```bash
src/venv-unified/bin/python src/audio_processing/server.py
src/venv-unified/bin/python src/audio_processing/server_posthoc.py
export PYTHONPATH=src/video_processing:src/video_processing/yolo_head   # video only
src/venv-unified/bin/python src/video_processing/server.py
src/venv-unified/bin/python src/video_processing/server_posthoc.py
```

**Mic/camera over plain HTTP** (LAN testing): Chrome blocks capture on
insecure origins. Add your server's address at
`chrome://flags/#unsafely-treat-insecure-origin-as-secure` and relaunch.

### GPU memory and the post-hoc services

One card serves the live classes, the two post-hoc services, llama-server
and whatever experiment is running, so the post-hoc services claim GPU
memory only while a run is on:

- Model weights load on the first run, not at start-up
  (`src/video_processing/server_posthoc.py` `_load_once`,
  `src/audio_processing/server_posthoc.py` `get_semantic_model`). A freshly
  started service holds nothing on the card.
- A service with no run for `DC_POSTHOC_IDLE_RECYCLE_S` seconds (default
  180) exits and systemd restarts it. Exiting is the one way every CUDA
  allocation comes back. The queue treats a refused connection during the
  restart as "retry in 20 s" (`posthoc_queue._ServiceUnavailable`), not as a
  failed pod.
- Before loading, a run checks the card's free memory with `nvidia-smi`
  against `DC_AUDIO_POSTHOC_MIN_FREE_MIB` (8192) or
  `DC_VIDEO_POSTHOC_MIN_FREE_MIB` (14336). A short card hands the GPU lease
  back and the run defers with the usual `gpu_busy` reply instead of dying in
  a CUDA OOM (`gpu_lease.acquire_for_run(min_free_mib=...)`).
- A run in progress when a class starts is pre-empted and re-queued
  (`gpu_lease.PreemptionWatcher`).

### Load testing

`tools/loadgen/` replays a real recording into the live audio websocket as N
synthetic pods and measures caption lag. It refuses to start while a real
pod is live and must never run near a class; read its README first.

### MOSS end to end (the default post-hoc engine)

The default post-hoc run is
[MOSS-Transcribe-Diarize](https://huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize)
(OpenMOSS 0.9B, Apache-2.0): one model listens to the whole recording and
writes timestamped, speaker-labelled, punctuated turns, so there is no
separate diarizer, aligner or word-assignment step. Its labels (S01, S02,
...) are named against the enrolled voice prints by
`asr_connectors/moss_naming.py`.

- Engine: `asr_connectors/moss_asr.py` runs `moss_cli.py` in
  `src/venv-granite` (transformers 5.x, the OpenMOSS package from a checkout
  of MOSS-Transcribe-Diarize, Triton) through `moss_run.py`.
- The decode loop is our own (`moss_cli.py`): the model in bf16 (Qwen3's MLP
  overflows fp16 on an hour of audio and emits only "!"), a pre-allocated
  fp16 KV cache, a Triton split-key kernel for the single-query step
  (`triton_decode.py`), the step replayed as a CUDA graph, and the vocabulary
  projection in fp32. On a Quadro RTX 8000 (Turing) it does about 42
  tokens/s, an hour of audio in 16 minutes.
- Turing GPUs have no PyTorch flash attention; `flash_turing_attn.py`
  registers the [flash-attention-turing](https://github.com/ssiu/flash-attention-turing)
  port as the `turing_fa` backend for the encoder and prefill. Build it once
  into the venv: clone it (or set `FLASH_ATTENTION_TURING_DIR`), then
  `MAX_JOBS=4 src/venv-granite/bin/pip install -v . --no-build-isolation`.
  On an Ampere-or-newer GPU the loop runs on PyTorch's own kernels without
  it.
- Budget: the cache is sized at launch for `--max-new-tokens` (default
  40,000, about 10 tokens per audio second); a recording past 70 minutes
  needs a larger value. Progress is written to `<out>.progress` while a pass
  runs.
- Naming: a cluster is named on up to 120 s of its longest turns against the
  students' voice prints of both laptops of the group (the enrollment print,
  else a print cut from the student's confidently named live rows); the best
  print wins above its kind's floor (0.35 enrollment, 0.60 session) and by a
  0.06 margin. The processor keeps these names (`named_by_asr`) instead of
  re-checking each piece against the prints.

## Open items

- Decide whether the account-less student-dashboard and expert-rating
  surface needs a credential. Those flows read transcripts, synthesized
  feedback and LLM answers through the passcode/alias routes, each marked
  "open by design" in the route comments. Tightening it means minting a
  token from the passcode that those flows can hold; a login gate would
  break them.
- Pod processing keys appear in nginx access logs via the `?key=` query
  parameter that `<video>` and `<img>` tags use against the guarded device
  endpoints. Either accept (keys are per pod and short-lived), scrub the
  parameter from nginx's log format, or rotate keys on session end.
- The live audio and video websocket services bind every interface; the
  other services bind loopback. Bind them to 127.0.0.1 so nginx is the only
  way in.
