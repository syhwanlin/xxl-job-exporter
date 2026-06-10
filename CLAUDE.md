# xxl-job-exporter

XXL-JOB Prometheus exporter，直接讀取 xxl-job MySQL/MariaDB，無需修改 xxl-job 本身。

## 專案結構

- `exporter.py` — exporter 本體（單檔 Python）
- `docker-compose.yml` — 最小部署（只跑 exporter，需要 .env）
- `docker-compose.demo.yml` — 完整 demo（MySQL + xxl-job-admin + simulator + exporter）
- `demo/` — demo 用的 init.sql、simulator.py、Dockerfile.simulator
- `kubernetes/` — Deployment + ServiceMonitor
- `prometheus/` — scrape config + 告警規則
- `grafana/` — dashboard JSON

## 本機開發

```bash
# 啟動完整 demo 環境（不需要現有 DB）
docker compose -f docker-compose.demo.yml up

# 停止並清除（含 volume）
docker compose -f docker-compose.demo.yml down -v
```

Demo 包含：MySQL、xxl-job-admin（http://localhost:8080/xxl-job-admin，admin/123456）、job simulator、exporter（http://localhost:9588/metrics）。

## CI/CD

| 觸發 | Workflow | 內容 |
|---|---|---|
| push / PR 到 main | `.github/workflows/ci.yml` | ruff lint + Docker build |
| push `v*` tag | `.github/workflows/publish.yml` | build amd64+arm64 image，推到 Docker Hub |

### 發版

```bash
git tag v1.x.x && git push origin v1.x.x
```

自動產生 Docker Hub tags：`vX.Y.Z`、`vX.Y`、`latest`。

### GitHub Secrets（已設定）

- `DOCKERHUB_USERNAME` — Docker Hub 帳號
- `DOCKERHUB_TOKEN` — Docker Hub Access Token（Read & Write）

## Docker Hub

`syhwanlin/xxl-job-exporter`
