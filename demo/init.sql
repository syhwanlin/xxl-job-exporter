CREATE DATABASE IF NOT EXISTS `xxl_job` DEFAULT CHARACTER SET utf8mb4;
USE `xxl_job`;

CREATE TABLE `xxl_job_info` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `job_group` int(11) NOT NULL,
  `job_desc` varchar(255) NOT NULL,
  `add_time` datetime DEFAULT NULL,
  `update_time` datetime DEFAULT NULL,
  `author` varchar(64) DEFAULT NULL,
  `alarm_email` varchar(255) DEFAULT NULL,
  `schedule_type` varchar(50) NOT NULL DEFAULT 'NONE',
  `schedule_conf` varchar(128) DEFAULT NULL,
  `misfire_strategy` varchar(50) NOT NULL DEFAULT 'DO_NOTHING',
  `executor_route_strategy` varchar(50) DEFAULT NULL,
  `executor_handler` varchar(255) DEFAULT NULL,
  `executor_param` varchar(512) DEFAULT NULL,
  `executor_block_strategy` varchar(50) DEFAULT NULL,
  `executor_timeout` int(11) NOT NULL DEFAULT '0',
  `executor_fail_retry_count` int(11) NOT NULL DEFAULT '0',
  `glue_type` varchar(50) NOT NULL DEFAULT 'BEAN',
  `glue_source` mediumtext,
  `glue_remark` varchar(128) DEFAULT NULL,
  `glue_updatetime` datetime DEFAULT NULL,
  `child_jobid` varchar(255) DEFAULT NULL,
  `trigger_status` tinyint(4) NOT NULL DEFAULT '0',
  `trigger_last_time` bigint(13) NOT NULL DEFAULT '0',
  `trigger_next_time` bigint(13) NOT NULL DEFAULT '0',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `xxl_job_log` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `job_group` int(11) NOT NULL,
  `job_id` int(11) NOT NULL,
  `executor_address` varchar(255) DEFAULT NULL,
  `executor_handler` varchar(255) DEFAULT NULL,
  `executor_param` varchar(512) DEFAULT NULL,
  `executor_sharding_param` varchar(20) DEFAULT NULL,
  `executor_fail_retry_count` int(11) NOT NULL DEFAULT '0',
  `trigger_time` datetime DEFAULT NULL,
  `trigger_code` int(11) NOT NULL,
  `trigger_msg` text,
  `handle_time` datetime DEFAULT NULL,
  `handle_code` int(11) NOT NULL,
  `handle_msg` text,
  `alarm_status` tinyint(4) NOT NULL DEFAULT '0',
  PRIMARY KEY (`id`),
  KEY `I_trigger_time` (`trigger_time`),
  KEY `I_handle_code` (`handle_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `xxl_job_log_report` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `trigger_day` datetime DEFAULT NULL,
  `running_count` int(11) NOT NULL DEFAULT '0',
  `suc_count` int(11) NOT NULL DEFAULT '0',
  `fail_count` int(11) NOT NULL DEFAULT '0',
  `update_time` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `i_trigger_day` (`trigger_day`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `xxl_job_logglue` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `job_id` int(11) NOT NULL,
  `glue_type` varchar(50) DEFAULT NULL,
  `glue_source` mediumtext,
  `glue_remark` varchar(128) NOT NULL,
  `add_time` datetime DEFAULT NULL,
  `update_time` datetime DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `xxl_job_registry` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `registry_group` varchar(50) NOT NULL,
  `registry_key` varchar(255) NOT NULL,
  `registry_value` varchar(255) NOT NULL,
  `update_time` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `i_g_k_v` (`registry_group`,`registry_key`,`registry_value`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `xxl_job_group` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `app_name` varchar(64) NOT NULL,
  `title` varchar(12) NOT NULL,
  `address_type` tinyint(4) NOT NULL DEFAULT '0',
  `address_list` text DEFAULT NULL,
  `update_time` datetime DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `xxl_job_user` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `username` varchar(50) NOT NULL,
  `password` varchar(50) NOT NULL,
  `role` tinyint(4) NOT NULL,
  `permission` varchar(255) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `i_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `xxl_job_lock` (
  `lock_name` varchar(50) NOT NULL,
  PRIMARY KEY (`lock_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- admin user (password: 123456)
INSERT INTO `xxl_job_user` (`username`, `password`, `role`) VALUES ('admin', 'e10adc3949ba59abbe56e057f20f883e', 1);

INSERT INTO `xxl_job_lock` (`lock_name`) VALUES ('schedule_lock'), ('fire_lock'), ('registry_lock');

-- read-only exporter account
CREATE USER IF NOT EXISTS 'xxl_job_exporter'@'%' IDENTIFIED BY 'exporter_pass';
GRANT SELECT ON xxl_job.* TO 'xxl_job_exporter'@'%';
FLUSH PRIVILEGES;
