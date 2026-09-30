# BLINC observability

Metrics from every BLINC process, scraped by Prometheus, charted in Grafana,
routed by Alertmanager. Everything listens on `127.0.0.1` only: reach
Grafana over the tailnet or `ssh -L 3000:127.0.0.1:3000 glamdring-vm-1`;
an nginx route (with auth) can be added later.

| Component | Where | Port |
|---|---|---|
| Prometheus (30 d retention) | compose, host network | 127.0.0.1:9090 |
| Grafana (dashboard `BLINC overview`, folder BLINC) | compose | 127.0.0.1:3000 |
| Alertmanager | compose | 127.0.0.1:9093 |
| node_exporter | compose | 127.0.0.1:9100 |
| API `/metrics` (gunicorn workers) | `blinc-discussion-capture.service` | 127.0.0.1:5001 (`DC_PORT`) |
| coordinator | `blinc-coordinator.service` | 127.0.0.1:9110 |
| live audio | `blinc-audio-processor.service` | 127.0.0.1:9111 |
| live video | `blinc-video-processor.service` | 127.0.0.1:9112 (`DC_VIDEO_METRICS_PORT`) |
| audio post-hoc | `blinc-audio-posthoc-processor.service` | 127.0.0.1:9113 |
| video post-hoc | `blinc-video-posthoc-processor.service` | 127.0.0.1:9114 |
| GPU (NVML) | `blinc-gpu-exporter.service` (this directory) | 127.0.0.1:9115 |
| dcgm-exporter (optional, `--profile dcgm`) | compose | 127.0.0.1:9400 |

Exporter ports come from `src/common/blinc_metrics.DEFAULT_PORTS`;
`BLINC_METRICS_PORT=<n>` in a unit's environment overrides its port and
`BLINC_METRICS_PORT=0` disables that unit's exporter. A second instance
beside production (another `DC_PORT`) sets its own ports and gets its own
gunicorn multiprocess directory (`/dev/shm/blinc-prometheus-<DC_PORT>`).

## Starting the stack (not started by the build; run these yourself)

```sh
cd /home/vlj9405/code/chemistry-dashboard/deploy/monitoring
cp .env.example .env            # ALERT_WEBHOOK_URL, GRAFANA_ADMIN_PASSWORD
docker compose config >/dev/null   # validates the compose file + .env
docker compose up -d
docker compose ps
curl -s 127.0.0.1:9090/-/ready; curl -s 127.0.0.1:9093/-/ready; curl -sI 127.0.0.1:3000/login | head -1
```

Targets page: <http://127.0.0.1:9090/targets>. Everything but `dcgm` should
be UP once the services run the instrumented code:

```sh
sudo cp blinc-gpu-exporter.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now blinc-gpu-exporter.service
curl -s 127.0.0.1:9115/metrics | grep blinc_gpu_memory
sudo systemctl restart blinc-discussion-capture blinc-coordinator blinc-audio-processor \
     blinc-video-processor blinc-audio-posthoc-processor blinc-video-posthoc-processor
for p in 5001 9110 9111 9112 9113 9114; do curl -s 127.0.0.1:$p/metrics | grep -c '^blinc_'; done
```

Reload after editing `prometheus.yml` or `rules/`: `curl -X POST 127.0.0.1:9090/-/reload`
(lifecycle API is enabled). Grafana re-reads `grafana/dashboards/*.json` every 30 s.

Stop / remove: `docker compose down` (data stays in the named volumes
`blinc-monitoring_prometheus-data`, `..._grafana-data`, `..._alertmanager-data`;
`docker compose down -v` deletes them).

## Alerts

Rules live in `rules/blinc.rules.yml` (Prometheus evaluates every 15 s):

| Alert | Condition | Severity |
|---|---|---|
| `BlincTargetDown` | any scrape target (except dcgm) down 2 min | critical |
| `PosthocRunnerDead` | `blinc_posthoc_runner_alive == 0` 5 min | warning |
| `HostDiskLow` | `/` under 15 % free for 10 min | warning |
| `HostMemoryLow` | MemAvailable under 10 % for 10 min | warning |
| `GpuMemoryHigh` | GPU memory over 90 % for 5 min | warning |
| `LivePodWindowBacklog` | a pod's `blinc_audio_window_backlog_seconds > 30` for 2 min | warning |
| `AsrPoolDegraded` | every CrisperWhisper slot degraded for 1 min | critical |
| `AsrDegradedPods` | any pod told `asr_status=degraded` for 1 min | warning |
| `AudioIngestDropping` | ingest queue evicting chunks for 5 min | warning |
| `CallbackGiveUps` | any callback give-up in 10 min | critical |
| `ApiHigh5xxRate` | 5xx over 5 % of requests for 5 min (with traffic) | critical |
| `ApiDbPoolNearExhaustion` | over 40 pool connections checked out for 5 min | warning |
| `PosthocRunFailed` | a post-hoc run failed in the last hour | warning |
| `PosthocFailedKeysPresent` | `posthoc_failed:*` keys present 5 min | warning |
| `PosthocQueueBacklog` | more than 10 jobs queued for 1 h | warning |

`alertmanager.yml` routes everything to the `webhook` receiver
(`url_file: /etc/alertmanager/webhook_url`, written by Compose from
`ALERT_WEBHOOK_URL` in `.env`); criticals repeat hourly, the rest every
4 h; a down target inhibits that job's other alerts.

### Adding a receiver (email via MailerSend later)

1. Add the receiver under `receivers:` in `alertmanager.yml` (the commented
   `email` block is the MailerSend SMTP shape; put the password in a file and
   mount it, e.g. `./mailersend_password:/etc/alertmanager/mailersend_password:ro`
   in the `alertmanager` service).
2. Point a route at it: under `route.routes` add
   `- matchers: [severity = critical]` with `receiver: email`, or set
   `route.receiver: email` for everything.
3. `docker compose exec alertmanager amtool check-config /etc/alertmanager/alertmanager.yml`,
   then `docker compose restart alertmanager`.
4. Test with `curl -XPOST 127.0.0.1:9093/api/v2/alerts -H 'Content-Type: application/json' -d '[{"labels":{"alertname":"Test","severity":"critical"}}]'`.

## Metric names

All application metrics are prefixed `blinc_` and defined through
`src/common/blinc_metrics.py`. Every process exports
`blinc_process_rss_bytes`, `blinc_process_threads`, `blinc_process_open_fds`,
`blinc_process_uptime_seconds`, `blinc_process_cpu_seconds_total` and
`blinc_service_up`, labelled `service` (and `pid`; under gunicorn the `pid`
label is added by the multiprocess collector, one series per live worker).

**API** (`src/server/routes/metrics.py`, job `api`) —
`blinc_http_requests_total{route,method,status}`,
`blinc_http_request_duration_seconds{route}` (histogram), `blinc_rate_limit_hits_total{route}`,
`blinc_socketio_clients`, `blinc_db_pool_connections{state}`, plus the shared
Redis gauges `blinc_live_pods`, `blinc_posthoc_queue_jobs{state}`,
`blinc_gpu_lease_held{slot}`, `blinc_gpu_lease_holder{slot,service}`,
`blinc_posthoc_failed{device_id,scope}`, `blinc_posthoc_failed_keys`.
The route label is the matched Flask rule (`/api/v1/sessions/<session_id>`).
The gunicorn workers share one port, so `/metrics` uses prometheus_client's
multiprocess mode (`PROMETHEUS_MULTIPROC_DIR`, set and wiped by
`gunicorn.conf.py:on_starting`, dead workers cleared in `child_exit`) and any
worker answers for all of them. `/metrics` is loopback-only (`verify_local`).

**Coordinator** (`src/server/coordinator.py`, job `coordinator`) — the shared
Redis gauges above, `blinc_device_connections`, `blinc_posthoc_runner_alive`,
`blinc_scheduler_job_runs_total{job,outcome}`, `blinc_scheduler_job_last_run_timestamp_seconds{job}`.

**Live audio** (`src/audio_processing/live_metrics.py`, job `audio`) —
`blinc_audio_connections`, `blinc_audio_live_pods`,
`blinc_audio_chunks_received_total`, `blinc_audio_chunks_dropped_total`,
`blinc_audio_seconds_dropped_total`, `blinc_audio_ingest_queue_chunks{pod}`,
`blinc_audio_window_backlog_seconds{pod}` (windows queued x 12 s: the caption-lag proxy),
`blinc_audio_asr_degraded{pod}`, `blinc_asr_pool_degraded`, `blinc_asr_pool_size`,
`blinc_asr_slots{state=busy|free|alive|degraded}`, `blinc_asr_windows_waiting`,
`blinc_audio_callback_retry_queue`, `blinc_audio_callback_retries_total{outcome=delivered|gave_up|dropped}`.
The pool numbers are read through `live_metrics.pool_stats()`, which uses
`getattr` with defaults on `crisperwhisper_asr._pool`; when the ASR workstream
adds a `stats()` method, point `pool_stats()` at it.

**Live video** (`src/video_processing/server.py`, job `video`, port 9112; the
video workstream's own exporter, prometheus_client directly rather than the
wrapper) — `blinc_video_pods` (pods streaming video),
`blinc_video_batches_dropped_total` (frame batches evicted unprocessed since
start, monotonic across pods) and `blinc_video_decode_mode{pod}` (0 = software,
1 = CUDA). Its port/address come from `DC_VIDEO_METRICS_PORT` /
`DC_VIDEO_METRICS_ADDR` (not `BLINC_METRICS_PORT`). It has no `blinc_process_*`
series; the dashboard uses the library's default `process_resident_memory_bytes`
for that job.

**Post-hoc** (audio job `audio-posthoc`, video job `video-posthoc`; same
names, Prometheus' `job` label tells them apart) —
`blinc_posthoc_runs_in_progress`, `blinc_posthoc_lease_held`, `blinc_posthoc_connections`,
`blinc_posthoc_runs_total{outcome=ok|failed|preempted}`,
`blinc_posthoc_claims_total{result=ok|busy}`, `blinc_posthoc_gpu_busy_total`,
`blinc_posthoc_fragment_joins_total{result}` (audio only: the video service does not join fragments).

**GPU** (`gpu_exporter.py`, job `gpu`) — `blinc_gpu_memory_used_bytes{gpu,name}`,
`blinc_gpu_memory_total_bytes`, `blinc_gpu_utilization_ratio`, `blinc_gpu_temperature_celsius`,
`blinc_gpu_power_watts`, `blinc_gpu_processes`, `blinc_gpu_process_memory_bytes{gpu,pid,process}`.
This is NVML through `nvidia-ml-py` in the unified venv, run as a systemd unit
because it could be verified without starting a container. Docker's daemon does
list the `nvidia` runtime, so `docker compose --profile dcgm up -d` may work as
well; it was not tried.

**Host** — standard node_exporter (`node_*`).

## Adding a metric

```python
import blinc_metrics as bm                          # src/common
REQS = bm.counter('thing_events_total', 'What it counts', ('label',))
REQS.labels('x').inc()
bm.callback_gauge('thing_depth', 'Read at scrape time', lambda: len(queue))
```

Names get the `blinc_` prefix automatically; a second call with the same name
returns the same metric. Without `prometheus_client` every helper returns a
no-op, so the services never depend on it. `tests/test_metrics.py` checks the
dashboard and rules only reference names the code exports; add new names to
the code first and the test will pick them up.
