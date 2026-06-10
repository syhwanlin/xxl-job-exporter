#!/usr/bin/env python3
import os
import random
import time
import logging
from datetime import datetime, timedelta

import pymysql
import pymysql.cursors

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB = dict(
    host=os.environ.get("DB_HOST", "127.0.0.1"),
    port=int(os.environ.get("DB_PORT", "3306")),
    user=os.environ.get("DB_USER", "root"),
    password=os.environ.get("DB_PASSWORD", "xxljob"),
    database=os.environ.get("DB_NAME", "xxl_job"),
    charset="utf8mb4",
    autocommit=True,
    cursorclass=pymysql.cursors.DictCursor,
)

EXECUTORS = [
    {"app_name": "order-service",   "title": "訂單服務", "instances": ["10.0.1.1:9999", "10.0.1.2:9999"]},
    {"app_name": "payment-service", "title": "金流服務", "instances": ["10.0.2.1:9999"]},
    {"app_name": "report-service",  "title": "報表服務", "instances": ["10.0.3.1:9999"]},
]

JOBS_DEF = [
    {"app_name": "order-service",   "job_desc": "同步訂單狀態", "handler": "syncOrderStatusHandler",     "cron": "0 * * * * ?",    "fail_rate": 0.05, "dur": (1, 5)},
    {"app_name": "order-service",   "job_desc": "清除過期訂單", "handler": "cleanExpiredOrdersHandler",  "cron": "0 */10 * * * ?", "fail_rate": 0.20, "dur": (2, 8)},
    {"app_name": "order-service",   "job_desc": "訂單報表匯出", "handler": "orderReportExportHandler",   "cron": "0 0 2 * * ?",    "fail_rate": 0.10, "dur": (30, 90)},
    {"app_name": "payment-service", "job_desc": "對帳金流記錄", "handler": "reconcilePaymentsHandler",   "cron": "0 */5 * * * ?",  "fail_rate": 0.15, "dur": (3, 12)},
    {"app_name": "payment-service", "job_desc": "扣款訂閱費",   "handler": "chargeSubscriptionsHandler", "cron": "0 0 * * * ?",    "fail_rate": 0.05, "dur": (5, 20)},
    {"app_name": "report-service",  "job_desc": "產生日報表",   "handler": "generateDailyReportHandler", "cron": "0 0 1 * * ?",    "fail_rate": 0.10, "dur": (60, 300)},
    {"app_name": "report-service",  "job_desc": "發送郵件摘要", "handler": "sendEmailDigestHandler",     "cron": "0 0 8 * * ?",    "fail_rate": 0.08, "dur": (2, 10)},
]


def connect_with_retry():
    for i in range(40):
        try:
            conn = pymysql.connect(**DB)
            log.info("connected to MySQL")
            return conn
        except Exception as e:
            log.info("waiting for MySQL (%d/40): %s", i + 1, e)
            time.sleep(3)
    raise RuntimeError("MySQL not available after 120s")


def setup(conn):
    with conn.cursor() as cur:
        for ex in EXECUTORS:
            cur.execute("SELECT id FROM xxl_job_group WHERE app_name=%s", (ex["app_name"],))
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO xxl_job_group (app_name, title, address_type, update_time) VALUES (%s,%s,0,NOW())",
                    (ex["app_name"], ex["title"]),
                )

        cur.execute("SELECT id, app_name FROM xxl_job_group")
        groups = {row["app_name"]: row["id"] for row in cur.fetchall()}

        for jd in JOBS_DEF:
            gid = groups.get(jd["app_name"])
            if gid is None:
                continue
            cur.execute(
                "SELECT id FROM xxl_job_info WHERE job_group=%s AND executor_handler=%s",
                (gid, jd["handler"]),
            )
            if not cur.fetchone():
                cur.execute(
                    """INSERT INTO xxl_job_info
                       (job_group, job_desc, add_time, update_time, author,
                        schedule_type, schedule_conf, misfire_strategy,
                        executor_route_strategy, executor_handler, executor_block_strategy,
                        executor_timeout, executor_fail_retry_count, glue_type,
                        trigger_status, trigger_last_time, trigger_next_time)
                       VALUES (%s,%s,NOW(),NOW(),'simulator','CRON',%s,'DO_NOTHING',
                               'FIRST',%s,'SERIAL_EXECUTION',0,0,'BEAN',1,0,0)""",
                    (gid, jd["job_desc"], jd["cron"], jd["handler"]),
                )

        cur.execute("SELECT id, executor_handler, job_group FROM xxl_job_info")
        jobs = cur.fetchall()

    return groups, jobs


def update_heartbeats(conn, groups):
    with conn.cursor() as cur:
        for ex in EXECUTORS:
            for addr in ex["instances"]:
                cur.execute(
                    "SELECT id FROM xxl_job_registry "
                    "WHERE registry_group='EXECUTOR' AND registry_key=%s AND registry_value=%s",
                    (ex["app_name"], addr),
                )
                row = cur.fetchone()
                if row:
                    cur.execute("UPDATE xxl_job_registry SET update_time=NOW() WHERE id=%s", (row["id"],))
                else:
                    cur.execute(
                        "INSERT INTO xxl_job_registry (registry_group,registry_key,registry_value,update_time) "
                        "VALUES ('EXECUTOR',%s,%s,NOW())",
                        (ex["app_name"], addr),
                    )


def insert_log(conn, groups, jobs, offset_seconds=None):
    row = random.choice(jobs)
    jd = next((j for j in JOBS_DEF if j["handler"] == row["executor_handler"]), JOBS_DEF[0])
    app_name = next((k for k, v in groups.items() if v == row["job_group"]), "order-service")
    ex = next((e for e in EXECUTORS if e["app_name"] == app_name), EXECUTORS[0])
    addr = random.choice(ex["instances"])

    trigger_time = datetime.now() - timedelta(seconds=offset_seconds or 0)

    r = random.random()
    if r < 0.05:
        trigger_code, handle_code, handle_time, handle_msg = 200, 0, None, None
        status = "running"
    elif r < 0.05 + jd["fail_rate"]:
        dur = random.randint(1, 8)
        trigger_code, handle_code = 200, 500
        handle_time = trigger_time + timedelta(seconds=dur)
        handle_msg = "job failed: " + random.choice([
            "connection timeout", "NullPointerException at line 42", "DB error: too many connections"
        ])
        status = "fail"
    else:
        dur = random.randint(*jd["dur"])
        trigger_code, handle_code = 200, 200
        handle_time = trigger_time + timedelta(seconds=dur)
        handle_msg = "execute complete, return: success"
        status = "success"

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO xxl_job_log
               (job_group, job_id, executor_address, executor_handler,
                trigger_time, trigger_code, trigger_msg,
                handle_time, handle_code, handle_msg, alarm_status)
               VALUES (%s,%s,%s,%s,%s,%s,'trigger success',%s,%s,%s,0)""",
            (row["job_group"], row["id"], addr, row["executor_handler"],
             trigger_time, trigger_code, handle_time, handle_code, handle_msg),
        )
    if offset_seconds is None:
        log.info("log: %-8s  job_id=%-3d  %s", status, row["id"], row["executor_handler"])


def seed_history(conn, groups, jobs):
    log.info("seeding historical data (200 entries across last 24h)...")
    offsets = (
        [random.randint(30, 290) for _ in range(20)]       # last 5 min
        + [random.randint(300, 3590) for _ in range(60)]   # last hour
        + [random.randint(3600, 86390) for _ in range(120)] # last 24h
    )
    for offset in offsets:
        insert_log(conn, groups, jobs, offset_seconds=offset)
    log.info("seed complete")


def main():
    conn = connect_with_retry()
    groups, jobs = setup(conn)
    log.info("setup: %d groups, %d jobs", len(groups), len(jobs))

    update_heartbeats(conn, groups)
    seed_history(conn, groups, jobs)

    tick = 0
    while True:
        time.sleep(5)
        tick += 1
        try:
            insert_log(conn, groups, jobs)
            if tick % 6 == 0:
                update_heartbeats(conn, groups)
                log.info("heartbeats refreshed")
        except Exception as e:
            log.error("error: %s", e)
            try:
                conn.close()
            except Exception:
                pass
            conn = connect_with_retry()


if __name__ == "__main__":
    main()
