# loadgen — how many pods can this server transcribe live?

`loadgen.py` replays a real recording into the live audio websocket as N
synthetic BYOD pods, speaking the browser client's protocol message for
message, and measures **caption lag** per pod while sampling the host. It is
meant to run **on the server** (it reads the audio service log, `nvidia-smi`,
`redis-cli` and `ss` locally).

## What it does to the system

- Logs in as the teacher you give it, creates ONE session named
  `LOADTEST <yyyymmdd hhmmss>` with `byod=true`, joins N pods to it
  (`loadgen1`, `loadgen2`, ...), streams audio, polls transcripts, and at exit
  (normal, Ctrl-C or SIGTERM) **ends and deletes that session** and its
  transcripts. With `--keep` the session is left for inspection: end and
  delete it yourself from the dashboard.
- It never touches any other session. The transcripts it creates are real
  rows until the delete; recordings are written under
  `src/audio_processing/recordings/<key> (...)_orig|_redu.wav` exactly as for
  a real pod (delete them by key if you want the disk back).
- N pods are N times real load: N CrisperWhisper windows every 12 s on the
  GPU, N ECAPA/feature pipelines on the CPU, N sockets through nginx.

## What NOT to do

- **Never run it during or shortly before a class.** It refuses to start when
  a real pod looks live (`live_pod:*` keys in Redis or an established
  connection into :9010), but only at start-up: a class joining mid-run will
  share a degraded server with your test.
- Do not point it at production without `--i-understand-this-loads-production`
  (it refuses), and do not put credentials on the command line of a shared
  shell; use `BLINC_LOADGEN_EMAIL` / `BLINC_LOADGEN_PASSWORD`.
- `--pods` is capped at 32.

## Running it

```sh
cd /home/vlj9405/code/chemistry-dashboard
export BLINC_LOADGEN_EMAIL='teacher@example.edu' BLINC_LOADGEN_PASSWORD='...'
# plan only, no network:
src/venv-unified/bin/python tools/loadgen/loadgen.py --dry-run \
  --audio "src/audio_processing/recordings/1078-578c6435-02e6-4e69-bcbf-18c91bb11825 (Tue Jul 21 19:52:20 2026)_redu.wav" \
  --ramp 2,4,8,12 --i-understand-this-loads-production
# the real thing (about 12 minutes for four 180 s steps):
src/venv-unified/bin/python tools/loadgen/loadgen.py \
  --audio "src/audio_processing/recordings/1078-578c6435-02e6-4e69-bcbf-18c91bb11825 (Tue Jul 21 19:52:20 2026)_redu.wav" \
  --ramp 2,4,8,12 --step-seconds 180 --max-lag 30 \
  --out tools/loadgen/results/$(date +%Y%m%d-%H%M%S).csv \
  --i-understand-this-loads-production
```

Any `_redu.wav` in `src/audio_processing/recordings` is a good input (16 kHz
mono of a real pod); anything ffmpeg can read works, it is converted once to
16 kHz mono float32 in memory. Each pod starts at a different offset into the
recording and loops it, so ASR windows are not in phase.

Options: `--pods N` for a single step, `--ramp 2,4,8,12,16` for cumulative
steps of `--step-seconds` (default 180) each, `--warmup-seconds` (default 60)
ignored at the start of each step for the verdict, `--keep`, `--dry-run`,
`--check-live` to run the live-pod checks on a non-production host, and
`--ws-url` when the audio socket is not at `<base-url>/audio_socket` (an
isolated test instance on other ports).

## Reading the results

One CSV row per pod every 5 s. Columns:

- `step`, `pods_target`, `step_elapsed_s`: where in the ramp the sample is.
- `audio_sent_s`: seconds of audio this pod has sent (all connections).
- `start_offset_s`: where the service placed the pod's first sample on the
  session clock (seconds since session creation), `audio_clock_s` = that plus
  audio sent on the current connection, `latest_transcript_end_s` = max
  `start_time + length` over the transcripts polled so far.
- `lag_s` = `audio_clock_s − latest_transcript_end_s`: how far the captions are
  behind the audio. A healthy server sits around 12–20 s (one 12 s ASR window
  plus inference); it climbs without bound once the GPU cannot keep up.
- `transcripts_total`, `degraded_events` (transitions into `asr_status:
  degraded`), `asr_state`, `reconnects`, `dropped_chunks` (client-side backlog
  overflow while disconnected), `poll_errors`.
- Host: `load1`, `mem_free_mb`, `gpu_util`, `gpu_mem_mb`, `drop_warnings`
  (cumulative count of the Phase 1 "ASR ingest queue full" / "CrisperWhisper
  ... dropped the window" WARNING lines in
  `src/audio_processing/audio_processing_service.log` since the run began).

`summary.json` next to the CSV (and the last lines on stdout) give per step
the median / p95 / max lag, the worst per-tick p95, degraded pods, drop
warnings, GPU utilisation and memory, load average, and a verdict: the largest
pod count whose p95 lag across pods stayed under `--max-lag` at every 5 s tick
after the warm-up.

## Notes on fidelity

- Mirrors `frontend/src/byod-join/byod-join-component.jsx`: the same REST join,
  the same `start` JSON (`start_time: 0`, `pcm_f32le`, 16 kHz, mono,
  `tag: true`, `numSpeakers: 0`), the roster confirmation right behind it, 4096
  float32 samples per binary frame every 0.256 s, transcript polls with
  `after_id` and the pod key header, full-jitter reconnects 1 → 30 s with a
  3 min deadline and a bounded offline backlog, and a plain close on exit.
- Pods carry no enrolled speakers (`collaborators: 0`), so speaker matching has
  nothing to compare against; that stage is slightly lighter than a real pod.
- Pods do not open the video socket; this measures the audio/ASR path only.
