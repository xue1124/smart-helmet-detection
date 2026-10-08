-- 在 DBeaver 中连接到 smart_construction 库后执行

-- 监控/分析会话表：RTSP、本地上传、ISAPI 回放均写入此表

USE smart_construction;

DROP TABLE IF EXISTS monitor_sessions;

CREATE TABLE monitor_sessions (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    device_id       BIGINT UNSIGNED NULL     COMMENT '关联 devices.id；手动 RTSP 可为 NULL',
    task_id         VARCHAR(220) NOT NULL COMMENT '任务目录名，对应 inference/no_helmet_tasks/{task_id}',
    device_name     VARCHAR(100) NOT NULL DEFAULT '' COMMENT '启动时设备/视频名称快照',
    source_type     VARCHAR(20)  NOT NULL DEFAULT 'rtsp'
        COMMENT 'rtsp=实时监控 local=本地上传 isapi=录像回放',
    status          VARCHAR(20)  NOT NULL DEFAULT 'streaming'
        COMMENT 'streaming/stopped/pending/downloading/running/completed/failed',
    frame_stride    SMALLINT     NOT NULL DEFAULT 25,
    enable_reid     TINYINT      NOT NULL DEFAULT 1 COMMENT '1=开启同人合并',
    reid_threshold  DECIMAL(4,2) NOT NULL DEFAULT 0.70,
    output_dir      VARCHAR(512) NOT NULL COMMENT 'crop 输出目录',
    error_message   TEXT         NULL     COMMENT '失败原因',
    started_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '开始时间',
    stopped_at      DATETIME     NULL     COMMENT '结束时间',
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_task_id (task_id),
    KEY idx_device_id (device_id),
    KEY idx_source_type (source_type),
    KEY idx_started_at (started_at),
    KEY idx_status (status),
    CONSTRAINT fk_monitor_sessions_device
        FOREIGN KEY (device_id) REFERENCES devices (id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='安全记录会话（RTSP/本地上传/ISAPI）';
