#!/usr/bin/env python3
"""
XXL-JOB Prometheus Exporter
===========================

A non-invasive Prometheus exporter for XXL-JOB (https://github.com/xuxueli/xxl-job).

It reads metrics directly from the XXL-JOB database (MySQL / MariaDB) using a
READ-ONLY account. It does NOT require any change to xxl-job-admin or executors,
and works with XXL-JOB 2.2.x / 2.3.x / 2.4.x / 3.x (schema auto-detected).

Status definitions follow xxl-job-admin's own JobLog page semantics:
  - success : handle_code = 200
  - running : trigger_code IN (0, 200) AND handle_code = 0
  - fail    : trigger_code NOT IN (0, 200) OR handle_code NOT IN (0, 200)

All configuration is via environment variables (see README.md / .env.example).
"""

import logging
import os
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pymysql
import pymysql.cursors
from prometheus_client import CONTENT_TYPE_LATEST
from prometheus_client.core import (
    CollectorRegistry,
    CounterMetricFamily,
    GaugeMetricFamily,
    InfoMetricFamily,
)
from prometheus_client.exposition import generate_latest

VERSION = "1.0.2"

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_duration(value: str) -> int:
    """Parse '5m' / '1h' / '24h' / '90s' / '7d' into seconds."""
    m = _DURATION_RE.match(value.strip())
    if not m:
        raise ValueError(f"Invalid duration: {value!r} (expected e.g. 30s, 5m, 1h, 7d)")
    return int(m.group(1)) * _DURATION_UNITS[m.group(2)]


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self) -> None:
        # --- Database ---
        self.db_host = _env("XXL_JOB_DB_HOST", "127.0.0.1")
        self.db_port = int(_env("XXL_JOB_DB_PORT", "3306"))
        self.db_user = _env("XXL_JOB_DB_USER", "xxl_job_exporter")
        self.db_name = _env("XXL_JOB_DB_NAME", "xxl_job")

        password_file = _env("XXL_JOB_DB_PASSWORD_FILE")
        if password_file:
            with open(password_file, "r", encoding="utf-8") as fh:
                self.db_password = fh.read().strip()
        else:
            self.db_password = _env("XXL_JOB_DB_PASSWORD", "")

        self.db_connect_timeout = int(_env("XXL_JOB_DB_CONNECT_TIMEOUT", "5"))
        self.db_read_timeout = int(_env("XXL_JOB_DB_READ_TIMEOUT", "10"))

        # --- Exporter behaviour ---
        self.listen_address = _env("EXPORTER_LISTEN_ADDRESS", "0.0.0.0")
        self.listen_port = int(_env("EXPORTER_LISTEN_PORT", "9588"))

        # Aggregation windows for execution counts / durations
        windows_raw = _env("EXPORTER_WINDOWS", "5m,1h,24h")
        self.windows = []
        for token in windows_raw.split(","):
            token = token.strip()
            if token:
                self.windows.append((token, _parse_duration(token)))
        if not self.windows:
            raise ValueError("EXPORTER_WINDOWS must contain at least one window")

        # How far back we look when computing "last success / last fail" and
        # the running-job scan. Bounds query cost on huge xxl_job_log tables.
        self.lookback_seconds = _parse_duration(_env("EXPORTER_LOOKBACK", "7d"))

        # Executor heartbeat timeout. xxl-job's RegistryConfig.DEAD_TIMEOUT = 90s.
        self.heartbeat_timeout = _parse_duration(_env("EXPORTER_HEARTBEAT_TIMEOUT", "90s"))

        # Cache TTL: 0 = query DB on every scrape; >0 = serve cached metrics
        # for that many seconds (protects the DB when several Prometheus
        # servers scrape the same exporter).
        self.cache_ttl = int(_env("EXPORTER_CACHE_TTL", "0"))

        # Label options
        self.include_job_desc = _env_bool("EXPORTER_INCLUDE_JOB_DESC", True)
        self.include_handler = _env_bool("EXPORTER_INCLUDE_HANDLER", True)
        self.label_max_length = int(_env("EXPORTER_LABEL_MAX_LENGTH", "120"))

        # Expose per-executor-instance heartbeat metric (address label can be
        # high-cardinality in very large clusters; disable if needed).
        self.per_instance_heartbeat = _env_bool("EXPORTER_PER_INSTANCE_HEARTBEAT", True)

        self.log_level = _env("EXPORTER_LOG_LEVEL", "INFO").upper()


# --------------------------------------------------------------------------- #
# SQL status conditions (mirror xxl-job-admin JobLog page filters)
# --------------------------------------------------------------------------- #

COND_SUCCESS = "handle_code = 200"
COND_RUNNING = "(trigger_code IN (0, 200) AND handle_code = 0)"
COND_FAIL = "(trigger_code NOT IN (0, 200) OR handle_code NOT IN (0, 200))"


# --------------------------------------------------------------------------- #
# Collector
# --------------------------------------------------------------------------- #

class XxlJobCollector:
    """Custom collector: queries the xxl-job DB at scrape time (with optional TTL cache)."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.log = logging.getLogger("xxl-job-exporter")
        self._lock = threading.Lock()
        self._cache: list = []
        self._cache_time: float = 0.0
        self._scrape_errors_total = 0
        self._has_log_report_table: bool | None = None  # detected lazily

    # -- DB helpers --------------------------------------------------------- #

    def _connect(self):
        return pymysql.connect(
            host=self.cfg.db_host,
            port=self.cfg.db_port,
            user=self.cfg.db_user,
            password=self.cfg.db_password,
            database=self.cfg.db_name,
            charset="utf8mb4",
            connect_timeout=self.cfg.db_connect_timeout,
            read_timeout=self.cfg.db_read_timeout,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )

    @staticmethod
    def _query(conn, sql: str, params=None) -> list:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()

    def ping_db(self) -> bool:
        """Used by /readyz."""
        try:
            conn = self._connect()
            try:
                self._query(conn, "SELECT 1")
            finally:
                conn.close()
            return True
        except Exception:
            return False

    # -- Label helpers ------------------------------------------------------ #

    def _clean(self, value) -> str:
        if value is None:
            return ""
        text = str(value).replace("\n", " ").replace("\r", " ").strip()
        if len(text) > self.cfg.label_max_length:
            text = text[: self.cfg.label_max_length] + "..."
        return text

    def _job_labels(self) -> list:
        labels = ["job_id", "app_name"]
        if self.cfg.include_job_desc:
            labels.append("job_desc")
        if self.cfg.include_handler:
            labels.append("handler")
        return labels

    def _job_label_values(self, job: dict, app_name: str) -> list:
        values = [str(job["id"]), app_name]
        if self.cfg.include_job_desc:
            values.append(self._clean(job.get("job_desc")))
        if self.cfg.include_handler:
            values.append(self._clean(job.get("executor_handler")))
        return values

    # -- Prometheus collect ------------------------------------------------- #

    def collect(self):
        with self._lock:  # single-flight: never run concurrent DB scrapes
            now = time.monotonic()
            if self.cfg.cache_ttl > 0 and self._cache and (now - self._cache_time) < self.cfg.cache_ttl:
                yield from self._cache
                return
            metrics = self._collect_now()
            self._cache = metrics
            self._cache_time = time.monotonic()
        yield from metrics

    def _collect_now(self) -> list:
        start = time.monotonic()
        up = GaugeMetricFamily(
            "xxl_job_up",
            "Whether the exporter could successfully query the xxl-job database (1 = yes).",
        )
        scrape_duration = GaugeMetricFamily(
            "xxl_job_scrape_duration_seconds",
            "Time taken to collect all xxl-job metrics from the database.",
        )
        scrape_errors = CounterMetricFamily(
            "xxl_job_scrape_errors_total",
            "Total number of scrapes that failed to query the database.",
        )
        info = InfoMetricFamily("xxl_job_exporter", "xxl-job exporter build info")
        info.add_metric([], {"version": VERSION})

        metrics: list = [info]
        try:
            conn = self._connect()
            try:
                metrics.extend(self._collect_from_db(conn))
            finally:
                conn.close()
            up.add_metric([], 1)
        except Exception:
            self.log.exception("scrape failed")
            self._scrape_errors_total += 1
            up.add_metric([], 0)

        scrape_errors.add_metric([], self._scrape_errors_total)
        scrape_duration.add_metric([], time.monotonic() - start)
        metrics.extend([up, scrape_errors, scrape_duration])
        return metrics

    # -- The actual queries -------------------------------------------------- #

    def _collect_from_db(self, conn) -> list:
        out: list = []
        cfg = self.cfg
        scrape_ts = time.time()  # wall clock used to convert SQL ages -> timestamps

        # ---------- 1. Groups (executors) ----------
        groups = self._query(
            conn,
            "SELECT id, app_name, title, address_type, address_list FROM xxl_job_group",
        )
        group_by_id = {g["id"]: g for g in groups}

        group_info = InfoMetricFamily(
            "xxl_job_group",
            "Executor groups registered in xxl-job-admin.",
        )
        manual_address_count = GaugeMetricFamily(
            "xxl_job_group_manual_address_count",
            "Number of manually-entered executor addresses (only for address_type=1 groups).",
            labels=["app_name", "title"],
        )
        for g in groups:
            group_info.add_metric(
                [],
                {
                    "app_name": self._clean(g["app_name"]),
                    "title": self._clean(g["title"]),
                    "address_type": "manual" if g["address_type"] == 1 else "auto",
                },
            )
            if g["address_type"] == 1:
                addresses = [a for a in (g.get("address_list") or "").split(",") if a.strip()]
                manual_address_count.add_metric(
                    [self._clean(g["app_name"]), self._clean(g["title"])], len(addresses)
                )
        out.extend([group_info, manual_address_count])

        # ---------- 2. Executor registry (auto-registered heartbeats) ----------
        registry_rows = self._query(
            conn,
            "SELECT registry_key, registry_value, "
            "       TIMESTAMPDIFF(SECOND, update_time, NOW()) AS age_seconds "
            "FROM xxl_job_registry WHERE registry_group = 'EXECUTOR'",
        )

        online_count = GaugeMetricFamily(
            "xxl_job_executor_online_count",
            f"Auto-registered executor instances with a heartbeat within the last "
            f"{cfg.heartbeat_timeout}s (xxl-job considers >90s dead).",
            labels=["app_name"],
        )
        registered_count = GaugeMetricFamily(
            "xxl_job_executor_registered_count",
            "All executor instances present in xxl_job_registry regardless of heartbeat age.",
            labels=["app_name"],
        )
        heartbeat_age = GaugeMetricFamily(
            "xxl_job_executor_heartbeat_age_seconds",
            "Seconds since the last heartbeat of each auto-registered executor instance.",
            labels=["app_name", "address"],
        )

        per_app_online: dict = {}
        per_app_total: dict = {}
        for row in registry_rows:
            app = self._clean(row["registry_key"])
            age = row["age_seconds"] if row["age_seconds"] is not None else float("inf")
            per_app_total[app] = per_app_total.get(app, 0) + 1
            if age <= cfg.heartbeat_timeout:
                per_app_online[app] = per_app_online.get(app, 0) + 1
            if cfg.per_instance_heartbeat:
                heartbeat_age.add_metric([app, self._clean(row["registry_value"])], float(age))

        # Ensure every auto-registered group appears (0 when nothing is online),
        # so absence-of-executor alerts work without absent().
        for g in groups:
            if g["address_type"] == 0:
                app = self._clean(g["app_name"])
                per_app_online.setdefault(app, 0)
                per_app_total.setdefault(app, 0)

        for app, count in per_app_online.items():
            online_count.add_metric([app], count)
        for app, count in per_app_total.items():
            registered_count.add_metric([app], count)
        out.extend([online_count, registered_count])
        if cfg.per_instance_heartbeat:
            out.append(heartbeat_age)

        # ---------- 3. Job definitions ----------
        jobs = self._query(
            conn,
            "SELECT id, job_group, job_desc, executor_handler, schedule_type, schedule_conf, "
            "       trigger_status, trigger_last_time, trigger_next_time "
            "FROM xxl_job_info",
        )

        job_labels = self._job_labels()
        trigger_status = GaugeMetricFamily(
            "xxl_job_job_trigger_status",
            "Job schedule status from xxl_job_info (1 = running/enabled, 0 = stopped).",
            labels=job_labels + ["schedule_type"],
        )
        trigger_last = GaugeMetricFamily(
            "xxl_job_job_trigger_last_timestamp_seconds",
            "Unix timestamp of the job's last schedule time (0 if never triggered).",
            labels=job_labels,
        )
        trigger_next = GaugeMetricFamily(
            "xxl_job_job_trigger_next_timestamp_seconds",
            "Unix timestamp of the job's next scheduled time (0 if not scheduled).",
            labels=job_labels,
        )
        jobs_total = GaugeMetricFamily(
            "xxl_job_jobs",
            "Number of jobs per executor group and schedule status.",
            labels=["app_name", "trigger_status"],
        )

        def app_of(job_group_id) -> str:
            g = group_by_id.get(job_group_id)
            return self._clean(g["app_name"]) if g else "unknown"

        agg: dict = {}
        job_meta: dict = {}  # job_id -> (label_values, app_name)
        for job in jobs:
            app = app_of(job["job_group"])
            lv = self._job_label_values(job, app)
            job_meta[job["id"]] = (lv, app)
            trigger_status.add_metric(
                lv + [self._clean(job.get("schedule_type") or "NONE")],
                float(job["trigger_status"]),
            )
            trigger_last.add_metric(lv, (job["trigger_last_time"] or 0) / 1000.0)
            trigger_next.add_metric(lv, (job["trigger_next_time"] or 0) / 1000.0)
            key = (app, str(job["trigger_status"]))
            agg[key] = agg.get(key, 0) + 1
        for (app, status), count in agg.items():
            jobs_total.add_metric([app, status], count)
        out.extend([trigger_status, trigger_last, trigger_next, jobs_total])

        # ---------- 4. Execution counts & durations per window ----------
        executions = GaugeMetricFamily(
            "xxl_job_job_executions",
            "Number of job executions per status within a trailing time window "
            "(status follows xxl-job-admin log page semantics).",
            labels=job_labels + ["status", "window"],
        )
        duration_avg = GaugeMetricFamily(
            "xxl_job_job_duration_seconds_avg",
            "Average duration (handle_time - trigger_time) of successful executions in the window.",
            labels=job_labels + ["window"],
        )
        duration_max = GaugeMetricFamily(
            "xxl_job_job_duration_seconds_max",
            "Maximum duration of successful executions in the window.",
            labels=job_labels + ["window"],
        )

        window_sql = f"""
            SELECT job_id, job_group,
                   SUM(CASE WHEN {COND_SUCCESS} THEN 1 ELSE 0 END) AS suc,
                   SUM(CASE WHEN {COND_FAIL}    THEN 1 ELSE 0 END) AS fail,
                   SUM(CASE WHEN {COND_RUNNING} THEN 1 ELSE 0 END) AS running,
                   AVG(CASE WHEN {COND_SUCCESS} AND handle_time IS NOT NULL AND trigger_time IS NOT NULL
                            THEN TIMESTAMPDIFF(SECOND, trigger_time, handle_time) END) AS dur_avg,
                   MAX(CASE WHEN {COND_SUCCESS} AND handle_time IS NOT NULL AND trigger_time IS NOT NULL
                            THEN TIMESTAMPDIFF(SECOND, trigger_time, handle_time) END) AS dur_max
            FROM xxl_job_log
            WHERE trigger_time >= DATE_SUB(NOW(), INTERVAL %s SECOND)
            GROUP BY job_id, job_group
        """
        for window_name, window_seconds in cfg.windows:
            rows = self._query(conn, window_sql, (window_seconds,))
            for row in rows:
                meta = job_meta.get(row["job_id"])
                if meta:
                    lv, _app = meta
                else:  # job deleted but logs remain
                    lv = self._job_label_values(
                        {"id": row["job_id"], "job_desc": "(deleted)", "executor_handler": ""},
                        app_of(row["job_group"]),
                    )
                executions.add_metric(lv + ["success", window_name], float(row["suc"] or 0))
                executions.add_metric(lv + ["fail", window_name], float(row["fail"] or 0))
                executions.add_metric(lv + ["running", window_name], float(row["running"] or 0))
                if row["dur_avg"] is not None:
                    duration_avg.add_metric(lv + [window_name], float(row["dur_avg"]))
                if row["dur_max"] is not None:
                    duration_max.add_metric(lv + [window_name], float(row["dur_max"]))
        out.extend([executions, duration_avg, duration_max])

        # ---------- 5. Last success / last fail per job (within lookback) ----------
        last_success = GaugeMetricFamily(
            "xxl_job_job_last_success_timestamp_seconds",
            f"Unix timestamp of the job's most recent successful execution "
            f"(scanned within the last {cfg.lookback_seconds}s).",
            labels=job_labels,
        )
        last_fail = GaugeMetricFamily(
            "xxl_job_job_last_fail_timestamp_seconds",
            f"Unix timestamp of the job's most recent failed execution "
            f"(scanned within the last {cfg.lookback_seconds}s).",
            labels=job_labels,
        )

        def emit_last(metric, condition, time_column):
            rows = self._query(
                conn,
                f"""
                SELECT job_id, job_group,
                       TIMESTAMPDIFF(SECOND, MAX({time_column}), NOW()) AS age_seconds
                FROM xxl_job_log
                WHERE trigger_time >= DATE_SUB(NOW(), INTERVAL %s SECOND) AND {condition}
                GROUP BY job_id, job_group
                """,
                (cfg.lookback_seconds,),
            )
            for row in rows:
                if row["age_seconds"] is None:
                    continue
                meta = job_meta.get(row["job_id"])
                if meta:
                    lv, _app = meta
                else:
                    lv = self._job_label_values(
                        {"id": row["job_id"], "job_desc": "(deleted)", "executor_handler": ""},
                        app_of(row["job_group"]),
                    )
                metric.add_metric(lv, scrape_ts - float(row["age_seconds"]))

        emit_last(last_success, COND_SUCCESS, "COALESCE(handle_time, trigger_time)")
        emit_last(last_fail, COND_FAIL, "COALESCE(handle_time, trigger_time)")
        out.extend([last_success, last_fail])

        # ---------- 6. Currently running / stuck executions ----------
        running_rows = self._query(
            conn,
            f"""
            SELECT job_id, job_group, COUNT(*) AS running_count,
                   TIMESTAMPDIFF(SECOND, MIN(trigger_time), NOW()) AS longest_seconds
            FROM xxl_job_log
            WHERE trigger_time >= DATE_SUB(NOW(), INTERVAL %s SECOND) AND {COND_RUNNING}
            GROUP BY job_id, job_group
            """,
            (cfg.lookback_seconds,),
        )
        running_now = GaugeMetricFamily(
            "xxl_job_job_running_executions",
            "Executions currently in 'running' state (triggered, no handle result yet).",
            labels=job_labels,
        )
        longest_running = GaugeMetricFamily(
            "xxl_job_job_longest_running_seconds",
            "Age of the oldest still-running execution of this job (stuck-job detection).",
            labels=job_labels,
        )
        for row in running_rows:
            meta = job_meta.get(row["job_id"])
            if meta:
                lv, _app = meta
            else:
                lv = self._job_label_values(
                    {"id": row["job_id"], "job_desc": "(deleted)", "executor_handler": ""},
                    app_of(row["job_group"]),
                )
            running_now.add_metric(lv, float(row["running_count"] or 0))
            if row["longest_seconds"] is not None:
                longest_running.add_metric(lv, float(row["longest_seconds"]))
        out.extend([running_now, longest_running])

        # ---------- 7. Daily report (cheap, from xxl_job_log_report) ----------
        if self._has_log_report_table is None:
            self._has_log_report_table = bool(
                self._query(
                    conn,
                    "SELECT 1 FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'xxl_job_log_report'",
                    (cfg.db_name,),
                )
            )
        if self._has_log_report_table:
            report = self._query(
                conn,
                "SELECT running_count, suc_count, fail_count FROM xxl_job_log_report "
                "WHERE DATE(trigger_day) = CURDATE() LIMIT 1",
            )
            today = GaugeMetricFamily(
                "xxl_job_today_executions",
                "Today's execution counts as maintained by xxl-job-admin's log report.",
                labels=["status"],
            )
            if report:
                today.add_metric(["running"], float(report[0]["running_count"]))
                today.add_metric(["success"], float(report[0]["suc_count"]))
                today.add_metric(["fail"], float(report[0]["fail_count"]))
            else:
                for status in ("running", "success", "fail"):
                    today.add_metric([status], 0.0)
            out.append(today)

        # ---------- 8. Log table size (cleanup monitoring) ----------
        size_rows = self._query(
            conn,
            "SELECT TABLE_ROWS, DATA_LENGTH + INDEX_LENGTH AS bytes "
            "FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'xxl_job_log'",
            (cfg.db_name,),
        )
        log_rows = GaugeMetricFamily(
            "xxl_job_log_table_rows_estimate",
            "Estimated row count of xxl_job_log (from information_schema; watch your log cleanup).",
        )
        log_bytes = GaugeMetricFamily(
            "xxl_job_log_table_size_bytes",
            "Approximate on-disk size (data + indexes) of xxl_job_log.",
        )
        if size_rows:
            log_rows.add_metric([], float(size_rows[0]["TABLE_ROWS"] or 0))
            log_bytes.add_metric([], float(size_rows[0]["bytes"] or 0))
        out.extend([log_rows, log_bytes])

        return out


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #

LANDING_PAGE = (
    "<html><head><title>XXL-JOB Exporter</title></head><body>"
    "<h1>XXL-JOB Prometheus Exporter</h1>"
    "<p><a href='/metrics'>/metrics</a> &mdash; Prometheus metrics</p>"
    "<p><a href='/healthz'>/healthz</a> &mdash; liveness probe</p>"
    "<p><a href='/readyz'>/readyz</a> &mdash; readiness probe (checks DB connectivity)</p>"
    "</body></html>"
).encode("utf-8")


def make_handler(registry: CollectorRegistry, collector: XxlJobCollector):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"xxl-job-exporter/{VERSION}"

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/metrics":
                try:
                    output = generate_latest(registry)
                except Exception:
                    logging.getLogger("xxl-job-exporter").exception("metrics generation failed")
                    self._respond(500, b"internal error", "text/plain")
                    return
                self._respond(200, output, CONTENT_TYPE_LATEST)
            elif path == "/healthz":
                self._respond(200, b"ok", "text/plain")
            elif path == "/readyz":
                if collector.ping_db():
                    self._respond(200, b"ok", "text/plain")
                else:
                    self._respond(503, b"database unreachable", "text/plain")
            elif path == "/":
                self._respond(200, LANDING_PAGE, "text/html; charset=utf-8")
            else:
                self._respond(404, b"not found", "text/plain")

        def _respond(self, code: int, body: bytes, content_type: str):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # route access logs through logging
            logging.getLogger("xxl-job-exporter.http").debug(fmt, *args)

    return Handler


def main() -> int:
    cfg = Config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("xxl-job-exporter")

    collector = XxlJobCollector(cfg)
    registry = CollectorRegistry()
    registry.register(collector)

    server = ThreadingHTTPServer(
        (cfg.listen_address, cfg.listen_port), make_handler(registry, collector)
    )

    def shutdown(signum, _frame):
        log.info("received signal %s, shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info(
        "xxl-job-exporter %s listening on %s:%d (db=%s@%s:%d/%s, windows=%s, lookback=%ss, cache_ttl=%ss)",
        VERSION, cfg.listen_address, cfg.listen_port,
        cfg.db_user, cfg.db_host, cfg.db_port, cfg.db_name,
        ",".join(w for w, _ in cfg.windows), cfg.lookback_seconds, cfg.cache_ttl,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
