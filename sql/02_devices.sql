-- 在 DBeaver / mysql 客户端连接到 smart_construction 库后执行
-- 全新库建表顺序：00 → 01 → 02 → 03

USE smart_construction;

CREATE TABLE IF NOT EXISTS devices (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    name            VARCHAR(100) NOT NULL COMMENT '设备名称',
    location        VARCHAR(200) NULL     COMMENT '安装位置',
    rtsp_url        VARCHAR(512) NOT NULL COMMENT 'RTSP 地址',
    status          SMALLINT     NOT NULL DEFAULT 0 COMMENT '0=未知 1=在线 2=离线',
    enabled         TINYINT      NOT NULL DEFAULT 1 COMMENT '1=启用 0=禁用',
    created_by      VARCHAR(64)  NULL     DEFAULT 'admin' COMMENT '创建人',
    remark          VARCHAR(500) NULL     COMMENT '备注',
    last_probe_at   DATETIME     NULL     COMMENT '最近探测时间',
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_enabled (enabled),
    KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='监控设备';
