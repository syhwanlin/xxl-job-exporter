# XXL-JOB Prometheus Exporter

非侵入式的 [XXL-JOB](https://github.com/xuxueli/xxl-job) Prometheus exporter。
**直接以唯讀帳號讀取 xxl-job 的 MySQL/MariaDB**，完全不需要修改 xxl-job-admin、執行器或任何既有設定，也不依賴 admin 的登入 API（避免版本升級時 API 變動造成監控中斷）。

- 支援 XXL-JOB **2.2.x / 2.3.x / 2.4.x / 3.x**（核心表結構皆相容，`xxl_job_log_report` 不存在時自動略過）
- 狀態判定與 xxl-job-admin「調度日誌」頁完全一致：
  - `success`：`handle_code = 200`
  - `running`：`trigger_code IN (0,200) AND handle_code = 0`
  - `fail`：`trigger_code NOT IN (0,200) OR handle_code NOT IN (0,200)`
- 所有查詢都走 `xxl_job_log` 既有索引（`I_trigger_time` / `I_handle_code`），並以 `EXPORTER_LOOKBACK` 限制掃描範圍，百萬級 log 表也安全

## 專案結構

```
xxl-job-exporter/
├── exporter.py                      # exporter 本體（單檔，無框架依賴）
├── requirements.txt
├── Dockerfile
├── docker-compose.yml               # 最小部署
├── .env.example                     # 全部設定項說明
├── sql/create_readonly_user.sql     # 建立唯讀 DB 帳號
├── prometheus/
│   ├── prometheus-scrape-config.yml # scrape 設定範例
│   └── alerts/xxl_job_alerts.yml    # 告警規則（含中文說明）
├── grafana/xxl_job_dashboard.json   # Grafana dashboard（直接 import）
└── kubernetes/
    ├── deployment.yaml              # Deployment + Service + Secret
    └── servicemonitor.yaml          # Prometheus Operator: ServiceMonitor + PrometheusRule
```

## 快速開始

### 1. 建立唯讀資料庫帳號（唯一需要在 DB 端做的事）

```bash
mysql -h <xxl-job-db-host> -u root -p < sql/create_readonly_user.sql
# 記得先改密碼，並建議限制來源網段
```

exporter 只需要 `SELECT` 權限，不會寫入任何資料。

### 2-A. Docker Compose

```bash
cp .env.example .env       # 填入 DB 連線資訊
docker compose up -d
curl http://localhost:9588/metrics
```

### 2-B. Kubernetes（GitOps 友善）

```bash
# 修改 kubernetes/deployment.yaml 中的 image、DB host 與 Secret
kubectl apply -f kubernetes/deployment.yaml
# Prometheus Operator 用戶（注意 release label 要符合你的 selector）：
kubectl apply -f kubernetes/servicemonitor.yaml
```

DB 密碼透過 `XXL_JOB_DB_PASSWORD_FILE` 從掛載的 Secret 讀取，不會出現在環境變數或 Pod spec 中。

### 2-C. 直接執行（不建議用於正式環境）

```bash
pip install -r requirements.txt
export XXL_JOB_DB_HOST=... XXL_JOB_DB_USER=... XXL_JOB_DB_PASSWORD=...
python exporter.py
```

### 3. Prometheus 與 Grafana

- 將 `prometheus/prometheus-scrape-config.yml` 的 scrape job 合併進你的設定，並載入 `alerts/xxl_job_alerts.yml`
- Grafana → Dashboards → Import → 上傳 `grafana/xxl_job_dashboard.json`

## HTTP 端點

| 端點 | 用途 |
|---|---|
| `/metrics` | Prometheus 指標 |
| `/healthz` | liveness（程序存活） |
| `/readyz` | readiness（實際對 DB 做 `SELECT 1`） |

## 指標一覽

### 任務定義（`xxl_job_info`）
| 指標 | 說明 |
|---|---|
| `xxl_job_job_trigger_status{job_id,app_name,job_desc,handler,schedule_type}` | 排程狀態（1=啟用, 0=停止） |
| `xxl_job_job_trigger_last_timestamp_seconds` | 上次調度時間（Unix 秒） |
| `xxl_job_job_trigger_next_timestamp_seconds` | 下次調度時間（Unix 秒）→ 偵測「錯過排程」 |
| `xxl_job_jobs{app_name,trigger_status}` | 各群組任務數 |

### 執行結果（`xxl_job_log`，依時間窗）
| 指標 | 說明 |
|---|---|
| `xxl_job_job_executions{...,status,window}` | 時間窗內 success / fail / running 次數（預設 5m,1h,24h） |
| `xxl_job_job_duration_seconds_avg{...,window}` | 成功執行平均耗時 |
| `xxl_job_job_duration_seconds_max{...,window}` | 成功執行最大耗時 |
| `xxl_job_job_last_success_timestamp_seconds` | 最近一次成功的時間 → 「太久沒成功」告警 |
| `xxl_job_job_last_fail_timestamp_seconds` | 最近一次失敗的時間 |
| `xxl_job_job_running_executions` | 目前 running 中的執行數 |
| `xxl_job_job_longest_running_seconds` | 最久的 running 執行已持續秒數 → 卡住偵測 |
| `xxl_job_today_executions{status}` | 今日統計（取自 `xxl_job_log_report`，零成本） |

> 已刪除但仍有 log 的任務會以 `job_desc="(deleted)"` 呈現，不會遺漏失敗紀錄。

### 執行器（`xxl_job_registry` / `xxl_job_group`）
| 指標 | 說明 |
|---|---|
| `xxl_job_executor_online_count{app_name}` | 心跳 90 秒內的在線實例數（與 xxl-job DEAD_TIMEOUT 一致；無在線時為 0，方便告警） |
| `xxl_job_executor_registered_count{app_name}` | registry 中全部實例數 |
| `xxl_job_executor_heartbeat_age_seconds{app_name,address}` | 各實例距上次心跳秒數 |
| `xxl_job_group_info{app_name,title,address_type}` | 群組資訊（auto / manual） |
| `xxl_job_group_manual_address_count` | 手動錄入群組的地址數 |

### Exporter 自身 / 維運
| 指標 | 說明 |
|---|---|
| `xxl_job_up` | 是否成功查詢 DB（0 時所有任務指標缺失，務必告警） |
| `xxl_job_scrape_duration_seconds` | 單次收集耗時 |
| `xxl_job_scrape_errors_total` | 累計收集失敗次數 |
| `xxl_job_log_table_rows_estimate` / `_size_bytes` | log 表大小 → 監控日誌清理是否正常 |

## 內建告警（`prometheus/alerts/xxl_job_alerts.yml`）

| 告警 | 條件 | 嚴重度 |
|---|---|---|
| `XxlJobExporterDown` | Prometheus 抓不到 exporter 3 分鐘 | critical |
| `XxlJobDatabaseUnreachable` | exporter 連不上 DB 3 分鐘 | critical |
| `XxlJobNoOnlineExecutor` | 某執行器群組 0 在線實例 2 分鐘 | critical |
| `XxlJobExecutorInstanceLost` | 在線數 < 註冊數 5 分鐘 | warning |
| `XxlJobExecutionFailed` | 任務近 5 分鐘有失敗 | warning |
| `XxlJobHighFailureRate` | 近 1 小時失敗率 > 50%（≥3 次執行） | critical |
| `XxlJobStuckRunning` | 有執行卡在 running 超過 1 小時 | warning |
| `XxlJobMissedSchedule` | 啟用中任務的 `trigger_next_time` 已過期 5 分鐘 → 調度器異常 | critical |
| `XxlJobScrapeSlow` | 收集耗時 > 10s | warning |
| `XxlJobLogTableTooLarge` | log 表估計 > 500 萬筆 | warning |

門檻值都在註解中說明，請依任務特性調整（例如每日批次可啟用範例中的 `XxlJobNoRecentSuccess`）。

## 設定項

全部透過環境變數設定，完整清單見 `.env.example`。重點：

| 變數 | 預設 | 說明 |
|---|---|---|
| `XXL_JOB_DB_HOST/PORT/USER/PASSWORD/NAME` | — | DB 連線；`XXL_JOB_DB_PASSWORD_FILE` 可改讀檔案（K8s Secret） |
| `EXPORTER_LISTEN_PORT` | `9588` | 監聽埠 |
| `EXPORTER_WINDOWS` | `5m,1h,24h` | 執行統計時間窗（告警的 `window` label 要對應） |
| `EXPORTER_LOOKBACK` | `7d` | last_success / running 掃描回看範圍（控制大表查詢成本） |
| `EXPORTER_HEARTBEAT_TIMEOUT` | `90s` | 執行器在線判定（= xxl-job DEAD_TIMEOUT） |
| `EXPORTER_CACHE_TTL` | `0` | >0 時快取指標 N 秒；多套 Prometheus 抓同一 exporter 時建議 15–30 |
| `EXPORTER_INCLUDE_JOB_DESC` | `true` | 任務名稱常改名會造成時序中斷，可關閉只留 `job_id` |
| `EXPORTER_PER_INSTANCE_HEARTBEAT` | `true` | 超大叢集可關閉逐實例心跳以降低 cardinality |

## 設計與維運注意事項

1. **效能**：每次 scrape 對 DB 的查詢約 6 + 時間窗數 次，全部命中索引。實測（含 3 個時間窗）單次收集 ~30ms。`scrape_interval` 建議 30s 即可，沒必要 15s。
2. **DB 保護**：exporter 內建 single-flight（同時只會有一個收集在跑）；`EXPORTER_CACHE_TTL` 可進一步防止多個 Prometheus 重複打 DB。`scrape_timeout` 請設定大於 `XXL_JOB_DB_READ_TIMEOUT`。
3. **多套 xxl-job**：一套 admin（一顆 DB）對應一個 exporter 實例，在 Prometheus scrape config 用 label（如 `xxl_job_env`）區分。
4. **計數語意**：`xxl_job_job_executions` 是「時間窗內的計數 gauge」而非 counter——因為 `xxl_job_log` 會被 admin 定期清理，counter 會出現非重啟性的下降導致 `rate()` 失真。告警直接用 `> 0` / 比例即可，不要套 `rate()`。
5. **時區**：所有時間差皆在 SQL 端以 `NOW()` 計算後換算成 Unix 時間，exporter 與 DB 主機時區不一致也不會出錯；但 xxl-job-admin 與 DB 本身仍應保持時區一致（這是 xxl-job 的既有要求）。
6. **安全**：容器以非 root（uid 10001）、read-only rootfs 執行；DB 帳號僅 `SELECT`。若 `/metrics` 暴露在不可信網段，建議前面加一層反向代理做認證或用 NetworkPolicy 限制。

## 本機驗證

本專案已在 MariaDB 10.11 + 官方 `tables_xxl_job.sql`（master 分支）schema 上完成整合測試，涵蓋：成功/失敗/執行中分類、心跳超時判定、卡住任務、已刪除任務 log、中文任務名稱、`xxl_job_log_report` 缺表降級。
