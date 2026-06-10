-- 為 exporter 建立「唯讀」帳號（最小權限，完全不影響 xxl-job 本身）
-- 請替換密碼與來源網段後，在 xxl-job 所在的 MySQL/MariaDB 執行。

CREATE USER 'xxl_job_exporter'@'%' IDENTIFIED BY 'CHANGE_ME_STRONG_PASSWORD';

-- 只需要 SELECT；exporter 不會寫入任何資料
GRANT SELECT ON xxl_job.* TO 'xxl_job_exporter'@'%';

-- （建議）限制來源 IP，例如只允許 exporter 所在網段：
-- CREATE USER 'xxl_job_exporter'@'10.0.0.%' IDENTIFIED BY '...';
-- GRANT SELECT ON xxl_job.* TO 'xxl_job_exporter'@'10.0.0.%';

FLUSH PRIVILEGES;
