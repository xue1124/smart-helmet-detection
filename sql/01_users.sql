-- 在 DBeaver 中连接到 smart_construction 库后执行
-- 默认管理员：admin / admin@2026

USE smart_construction;

CREATE TABLE IF NOT EXISTS users (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    username        VARCHAR(64)  NOT NULL COMMENT '登录名',
    password_hash   VARCHAR(128) NOT NULL COMMENT 'PBKDF2-SHA256 哈希',
    role            VARCHAR(32)  NOT NULL DEFAULT 'admin' COMMENT '角色',
    enabled         TINYINT      NOT NULL DEFAULT 1 COMMENT '1=启用',
    last_login_at   DATETIME     NULL     COMMENT '最近登录',
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统用户';

-- 密码 admin@2026（固定盐 PBKDF2-SHA256）
INSERT INTO users (username, password_hash, role, enabled)
VALUES (
    'admin',
    'a1b2c3d4e5f607081122334455667788$ab9d5d8841776bbd5ca641ef9b36a88047a1fa7d990a5448efaac80e3688ee57',
    'admin',
    1
)
ON DUPLICATE KEY UPDATE
    password_hash = VALUES(password_hash),
    role = VALUES(role),
    enabled = VALUES(enabled);
