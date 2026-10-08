-- 为已有 monitor_sessions 表增加来源字段（DBeaver 执行）
USE smart_construction;

ALTER TABLE monitor_sessions
    ADD COLUMN source_type VARCHAR(20) NOT NULL DEFAULT 'rtsp'
        COMMENT 'rtsp=实时监控 local=本地上传 isapi=录像回放'
        AFTER device_name;

CREATE INDEX idx_source_type ON monitor_sessions (source_type);
