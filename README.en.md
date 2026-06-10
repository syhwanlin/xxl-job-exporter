# XXL-JOB Prometheus Exporter

[![CI](https://github.com/syhwanlin/xxl-job-exporter/actions/workflows/ci.yml/badge.svg)](https://github.com/syhwanlin/xxl-job-exporter/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

[中文](./README.md) | English

A non-invasive Prometheus exporter for [XXL-JOB](https://github.com/xuxueli/xxl-job).
**Reads metrics directly from xxl-job's MySQL/MariaDB using a read-only account** — no changes to xxl-job-admin, executors, or any existing configuration required. It does not depend on the admin login API, so monitoring is never broken by xxl-job version upgrades.

- **xxl-job-admin has no native Prometheus metrics**: even with Spring Actuator + Micrometer added manually, you only get generic JVM / HTTP metrics — no scheduling business data (execution results, heartbeats, stuck-job detection, etc.)
- Supports XXL-JOB **2.2.x / 2.3.x / 2.4.x / 3.x** (core schema is compatible; `xxl_job_log_report` is skipped gracefully if absent)
- Job status follows the exact same semantics as the xxl-job-admin "Scheduling Log" page:
  - `success`: `handle_code = 200`
  - `running`: `trigger_code IN (0,200) AND handle_code = 0`
  - `fail`: `trigger_code NOT IN (0,200) OR handle_code NOT IN (0,200)`
- All queries use existing indexes on `xxl_job_log` (`I_trigger_time` / `I_handle_code`) and are bounded by `EXPORTER_LOOKBACK`, safe even on tables with millions of rows

## Project Structure

```
xxl-job-exporter/
├── exporter.py                      # exporter (single file, no framework dependencies)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml               # minimal deployment
├── .env.example                     # all configuration options with descriptions
├── sql/create_readonly_user.sql     # create the read-only DB account
├── prometheus/
│   ├── prometheus-scrape-config.yml # scrape config example
│   └── alerts/xxl_job_alerts.yml    # alerting rules
├── grafana/xxl_job_dashboard.json   # Grafana dashboard (ready to import)
└── kubernetes/
    ├── deployment.yaml              # Deployment + Service + Secret
    └── servicemonitor.yaml          # Prometheus Operator: ServiceMonitor + PrometheusRule
```

## Quick Start

### 1. Create a read-only database account (the only step required on the DB side)

```bash
mysql -h <xxl-job-db-host> -u root -p < sql/create_readonly_user.sql
# Remember to change the password and restrict the source IP range
```

The exporter only needs `SELECT` — it never writes any data.

### 2-A. Docker Compose

```bash
cp .env.example .env       # fill in your DB connection details
docker compose up -d
curl http://localhost:9588/metrics
```

### 2-B. Kubernetes (GitOps-friendly)

```bash
# Edit the image, DB host, and Secret in kubernetes/deployment.yaml first
kubectl apply -f kubernetes/deployment.yaml
# For Prometheus Operator users (ensure the release label matches your selector):
kubectl apply -f kubernetes/servicemonitor.yaml
```

The DB password is read from a mounted Secret via `XXL_JOB_DB_PASSWORD_FILE` and never appears in environment variables or the Pod spec.

### 2-C. Run directly (not recommended for production)

```bash
pip install -r requirements.txt
export XXL_JOB_DB_HOST=... XXL_JOB_DB_USER=... XXL_JOB_DB_PASSWORD=...
python exporter.py
```

### 3. Prometheus & Grafana

- Merge the scrape job from `prometheus/prometheus-scrape-config.yml` into your Prometheus config and load `alerts/xxl_job_alerts.yml`
- Grafana → Dashboards → Import → upload `grafana/xxl_job_dashboard.json`

## Local Demo (full environment)

No existing XXL-Job database needed — spin up a complete demo environment with one command:

```bash
docker compose -f docker-compose.demo.yml up
```

Once running, open:

| Service | URL | Description |
|---|---|---|
| Exporter metrics | http://localhost:9588/metrics | Prometheus metrics |
| Exporter home | http://localhost:9588/ | Endpoint index |
| XXL-Job Admin UI | http://localhost:8080/xxl-job-admin | Login: `admin` / `123456` |

What's included:
- **MySQL 8.0** — initializes the xxl-job schema
- **xxl-job-admin 2.4.1** — official Admin UI
- **job-simulator** — creates 3 executor groups (order / payment / report) and 7 jobs, seeds 200 historical log entries spread across the last 24 hours, then continuously inserts new success / fail / running records every 5 seconds
- **xxl-job-exporter** — this exporter, connected to a dedicated read-only DB account

> **Apple Silicon (M-series) users**: xxl-job-admin is an amd64 image running under Rosetta; allow 30–60 seconds for it to start.

Stop and clean up:
```bash
docker compose -f docker-compose.demo.yml down -v
```

## HTTP Endpoints

| Endpoint | Purpose |
|---|---|
| `/metrics` | Prometheus metrics |
| `/healthz` | Liveness probe (process alive) |
| `/readyz` | Readiness probe (runs `SELECT 1` against the DB) |

## Metrics Reference

### Job definitions (`xxl_job_info`)

| Metric | Description |
|---|---|
| `xxl_job_job_trigger_status{job_id,app_name,job_desc,handler,schedule_type}` | Schedule status (1 = enabled, 0 = stopped) |
| `xxl_job_job_trigger_last_timestamp_seconds` | Last scheduled time (Unix seconds) |
| `xxl_job_job_trigger_next_timestamp_seconds` | Next scheduled time (Unix seconds) → detect missed schedules |
| `xxl_job_jobs{app_name,trigger_status}` | Number of jobs per executor group |

### Execution results (`xxl_job_log`, per time window)

| Metric | Description |
|---|---|
| `xxl_job_job_executions{...,status,window}` | success / fail / running count within the trailing window (default: 5m, 1h, 24h) |
| `xxl_job_job_duration_seconds_avg{...,window}` | Average duration of successful executions in the window |
| `xxl_job_job_duration_seconds_max{...,window}` | Maximum duration of successful executions in the window |
| `xxl_job_job_last_success_timestamp_seconds` | Timestamp of the most recent successful execution → "no recent success" alerting |
| `xxl_job_job_last_fail_timestamp_seconds` | Timestamp of the most recent failed execution |
| `xxl_job_job_running_executions` | Number of currently running executions |
| `xxl_job_job_longest_running_seconds` | Age of the oldest still-running execution → stuck job detection |
| `xxl_job_today_executions{status}` | Today's totals from `xxl_job_log_report` (zero-cost query) |

> Jobs that have been deleted but still have log entries appear with `job_desc="(deleted)"` — no failure records are lost.

### Executors (`xxl_job_registry` / `xxl_job_group`)

| Metric | Description |
|---|---|
| `xxl_job_executor_online_count{app_name}` | Instances with a heartbeat within 90s (matches xxl-job's `DEAD_TIMEOUT`; reports 0 when none online, making absence alerts straightforward) |
| `xxl_job_executor_registered_count{app_name}` | All instances present in the registry regardless of heartbeat age |
| `xxl_job_executor_heartbeat_age_seconds{app_name,address}` | Seconds since each instance's last heartbeat |
| `xxl_job_group_info{app_name,title,address_type}` | Group metadata (auto / manual registration) |
| `xxl_job_group_manual_address_count` | Number of manually entered addresses for manual-mode groups |

### Exporter internals / operations

| Metric | Description |
|---|---|
| `xxl_job_up` | Whether the DB was queried successfully (alert on 0 — all job metrics will be absent) |
| `xxl_job_scrape_duration_seconds` | Time taken for a single collection |
| `xxl_job_scrape_errors_total` | Cumulative number of failed scrapes |
| `xxl_job_log_table_rows_estimate` / `_size_bytes` | Log table size → monitor whether log cleanup is working |

## Built-in Alerts (`prometheus/alerts/xxl_job_alerts.yml`)

| Alert | Condition | Severity |
|---|---|---|
| `XxlJobExporterDown` | Prometheus cannot reach the exporter for 3 minutes | critical |
| `XxlJobDatabaseUnreachable` | Exporter cannot reach the DB for 3 minutes | critical |
| `XxlJobNoOnlineExecutor` | An executor group has 0 online instances for 2 minutes | critical |
| `XxlJobExecutorInstanceLost` | Online count < registered count for 5 minutes | warning |
| `XxlJobExecutionFailed` | A job has failures in the last 5 minutes | warning |
| `XxlJobHighFailureRate` | Failure rate > 50% in the last hour (≥ 3 executions) | critical |
| `XxlJobStuckRunning` | An execution has been in the running state for over 1 hour | warning |
| `XxlJobMissedSchedule` | An enabled job's `trigger_next_time` is more than 5 minutes in the past → scheduler anomaly | critical |
| `XxlJobScrapeSlow` | Collection takes > 10s | warning |
| `XxlJobLogTableTooLarge` | Estimated log table row count > 5 million | warning |

All thresholds are documented in the alert file comments — adjust them to match your job characteristics (e.g. enable the `XxlJobNoRecentSuccess` example for daily batch jobs).

## Configuration

All settings are via environment variables. See `.env.example` for the full list. Key options:

| Variable | Default | Description |
|---|---|---|
| `XXL_JOB_DB_HOST/PORT/USER/PASSWORD/NAME` | — | DB connection; use `XXL_JOB_DB_PASSWORD_FILE` to read from a mounted file (K8s Secret) |
| `EXPORTER_LISTEN_PORT` | `9588` | Listen port |
| `EXPORTER_WINDOWS` | `5m,1h,24h` | Execution count time windows (the `window` label in alerts must match) |
| `EXPORTER_LOOKBACK` | `7d` | Look-back range for last_success / running scans (controls query cost on large tables) |
| `EXPORTER_HEARTBEAT_TIMEOUT` | `90s` | Executor online threshold (= xxl-job `DEAD_TIMEOUT`) |
| `EXPORTER_CACHE_TTL` | `0` | Cache metrics for N seconds when > 0; recommended 15–30 when multiple Prometheus instances scrape the same exporter |
| `EXPORTER_INCLUDE_JOB_DESC` | `true` | Disable to keep only `job_id` if job descriptions change frequently (avoids time-series churn) |
| `EXPORTER_PER_INSTANCE_HEARTBEAT` | `true` | Disable per-instance heartbeat metrics to reduce cardinality in very large clusters |

## Design & Operational Notes

1. **Performance**: Each scrape runs 6 + (number of windows) queries, all hitting existing indexes. Measured at ~30ms per collection with 3 windows. A `scrape_interval` of 30s is sufficient; 15s is unnecessary.
2. **DB protection**: The exporter has a built-in single-flight lock (only one collection runs at a time). `EXPORTER_CACHE_TTL` provides additional protection when multiple Prometheus servers scrape the same instance. Set `scrape_timeout` greater than `XXL_JOB_DB_READ_TIMEOUT`.
3. **Multiple xxl-job instances**: Deploy one exporter per admin instance (per DB). Use a label (e.g. `xxl_job_env`) in the Prometheus scrape config to distinguish them.
4. **Counter semantics**: `xxl_job_job_executions` is a **windowed gauge**, not a counter — because `xxl_job_log` is periodically purged by xxl-job-admin, which would cause non-restart drops that make `rate()` meaningless. Use `> 0` or ratio comparisons in alerts; do not apply `rate()`.
5. **Timezone**: All time deltas are computed in SQL using `NOW()` and converted to Unix timestamps, so mismatches between the exporter and DB host timezone are harmless. However, xxl-job-admin and the DB itself must remain in the same timezone — this is a pre-existing xxl-job requirement.
6. **Security**: The container runs as a non-root user (uid 10001) with a read-only rootfs. The DB account has `SELECT` only. If `/metrics` is exposed to an untrusted network, place an authenticating reverse proxy in front of it or restrict access with a NetworkPolicy.

## Local Validation

Tested against MariaDB 10.11 with the official `tables_xxl_job.sql` (master branch) schema, covering: success/fail/running classification, heartbeat timeout detection, stuck jobs, log entries for deleted jobs, multi-byte job names, and graceful degradation when `xxl_job_log_report` is absent.
