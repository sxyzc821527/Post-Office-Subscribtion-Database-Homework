-- ================================================================
-- 邮局报刊订阅系统 —— 核心权限管理模块 建表脚本
-- 对应 authority.py 的 INIT_SQL_LIST
-- 执行方式：mysql -u root -p < schema.sql
--          或在 Navicat / MySQL Workbench 中直接运行
-- ================================================================

-- 1. 创建数据库（如已存在请注释本行）
CREATE DATABASE IF NOT EXISTS `post_office`
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_general_ci;

USE `post_office`;

-- ----------------------------------------------------------------
-- 2. 员工表（节点 10）
-- ----------------------------------------------------------------
DROP TABLE IF EXISTS `sys_employee`;
CREATE TABLE `sys_employee` (
    `id`          BIGINT       NOT NULL AUTO_INCREMENT COMMENT '员工ID',
    `emp_no`      VARCHAR(32)  NOT NULL COMMENT '工号',
    `username`    VARCHAR(64)  NOT NULL COMMENT '登录名',
    `password`    VARCHAR(128) NOT NULL COMMENT '密码(加盐MD5)',
    `real_name`   VARCHAR(64)  DEFAULT NULL COMMENT '真实姓名',
    `phone`       VARCHAR(20)  DEFAULT NULL COMMENT '手机号',
    `status`      TINYINT      NOT NULL DEFAULT 1 COMMENT '1启用 0停用',
    `create_time` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `update_time` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_username` (`username`),
    UNIQUE KEY `uk_emp_no` (`emp_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='员工表';

-- ----------------------------------------------------------------
-- 3. 权限等级表（节点 17 / 20-25）
-- ----------------------------------------------------------------
DROP TABLE IF EXISTS `sys_auth_level`;
CREATE TABLE `sys_auth_level` (
    `level` VARCHAR(8)   NOT NULL COMMENT '等级编码 O5~O0',
    `name`  VARCHAR(64)  NOT NULL COMMENT '等级名称',
    `desc`  VARCHAR(255) DEFAULT NULL COMMENT '描述',
    PRIMARY KEY (`level`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='权限等级表';

-- ----------------------------------------------------------------
-- 4. 权限点表（菜单/按钮/接口/数据 四类，节点 51-55）
-- ----------------------------------------------------------------
DROP TABLE IF EXISTS `sys_permission`;
CREATE TABLE `sys_permission` (
    `id`   BIGINT       NOT NULL AUTO_INCREMENT,
    `code` VARCHAR(128) NOT NULL COMMENT '权限标识 如 menu:newspaper',
    `type` VARCHAR(16)  NOT NULL COMMENT 'menu/btn/api/data',
    `name` VARCHAR(128) DEFAULT NULL COMMENT '权限名称',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_code` (`code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='权限点表';

-- ----------------------------------------------------------------
-- 5. 员工-等级 分配表
-- ----------------------------------------------------------------
DROP TABLE IF EXISTS `sys_employee_level`;
CREATE TABLE `sys_employee_level` (
    `emp_id`     BIGINT     NOT NULL,
    `level`      VARCHAR(8) NOT NULL,
    `assign_time` DATETIME  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`emp_id`, `level`),
    KEY `idx_level` (`level`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='员工-等级分配表';

-- ----------------------------------------------------------------
-- 6. 员工-自定义权限 表（在等级之外额外授予/撤销权限）
-- ----------------------------------------------------------------
DROP TABLE IF EXISTS `sys_employee_permission`;
CREATE TABLE `sys_employee_permission` (
    `emp_id`    BIGINT       NOT NULL,
    `perm_code` VARCHAR(128) NOT NULL,
    `granted`   TINYINT      NOT NULL DEFAULT 1 COMMENT '1授予 0撤销',
    PRIMARY KEY (`emp_id`, `perm_code`),
    KEY `idx_emp` (`emp_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='员工自定义权限表';

-- ----------------------------------------------------------------
-- 7. 操作日志表（节点 103）
-- ----------------------------------------------------------------
DROP TABLE IF EXISTS `sys_operation_log`;
CREATE TABLE `sys_operation_log` (
    `id`         BIGINT      NOT NULL AUTO_INCREMENT,
    `emp_id`     BIGINT      DEFAULT NULL COMMENT '操作人ID',
    `emp_name`   VARCHAR(64) DEFAULT NULL,
    `module`     VARCHAR(64) DEFAULT NULL COMMENT '模块',
    `action`     VARCHAR(64) DEFAULT NULL COMMENT '操作',
    `detail`     TEXT        DEFAULT NULL COMMENT '详情(JSON)',
    `ip`         VARCHAR(64) DEFAULT NULL,
    `create_time` DATETIME   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_emp` (`emp_id`),
    KEY `idx_time` (`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='操作日志表';


-- ================================================================
-- 初始化数据
-- ================================================================

-- 标准权限等级（O5 ~ O0）
INSERT INTO `sys_auth_level`(`level`, `name`, `desc`) VALUES
('O5', '超级管理员',       '拥有系统全部权限'),
('O4', '报刊数据管理员',   '管理报刊数据、分类'),
('O3', '客户/订阅管理员',  '管理客户、处理订阅'),
('O2', '入库管理员',       '报刊入库、库存盘点'),
('O1', '发放员',           '报刊发放、签收确认'),
('O0', '财务对账员',       '订阅费用统计、对账');

-- 权限点（四类：menu / btn / api / data）
INSERT INTO `sys_permission`(`code`, `type`, `name`) VALUES
-- 菜单
('menu:newspaper',   'menu', '报刊数据'),
('menu:category',    'menu', '分类管理'),
('menu:customer',    'menu', '客户数据'),
('menu:subscription','menu', '订阅管理'),
('menu:stock',       'menu', '报刊入库'),
('menu:inventory',   'menu', '库存盘点'),
('menu:delivery',    'menu', '报刊发放'),
('menu:sign',        'menu', '签收确认'),
('menu:finance',     'menu', '费用统计'),
('menu:report',      'menu', '报表对账'),
('menu:employee',    'menu', '员工管理'),
('menu:authlevel',   'menu', '权限等级'),
('menu:log',         'menu', '操作日志'),
-- 按钮
('btn:newspaper:add',  'btn', '报刊-新增'),
('btn:newspaper:edit', 'btn', '报刊-编辑'),
('btn:newspaper:del',  'btn', '报刊-删除'),
('btn:stock:in',       'btn', '入库登记'),
('btn:stock:check',    'btn', '库存盘点'),
('btn:delivery:assign','btn', '任务分配'),
('btn:delivery:confirm','btn','签收确认'),
('btn:finance:stat',   'btn', '费用统计'),
('btn:finance:reconcile','btn','对账'),
-- 接口
('api:newspaper:*',  'api', '报刊接口'),
('api:category:*',   'api', '分类接口'),
('api:customer:*',   'api', '客户接口'),
('api:subscription:*','api','订阅接口'),
('api:stock:*',      'api', '入库接口'),
('api:inventory:*',  'api', '盘点接口'),
('api:delivery:*',   'api', '发放接口'),
('api:sign:*',       'api', '签收接口'),
('api:finance:*',    'api', '费用接口'),
('api:report:*',     'api', '报表接口'),
-- 数据
('data:newspaper:all','data','报刊-全部数据'),
('data:customer:all', 'data','客户-全部数据'),
('data:stock:all',    'data','库存-全部数据'),
('data:delivery:self','data','发放-仅本人任务'),
('data:finance:all',  'data','费用-全部数据');

-- ================================================================
-- 初始超级管理员账号
-- ================================================================
-- 默认账号：admin / admin123
-- 密码加盐规则与 authority.py 中 hash_password() 一致：
--   md5('admin123' + 'post_office_2026')
-- 下面这串就是 md5('admin123post_office_2026') 的结果
INSERT INTO `sys_employee`(`emp_no`, `username`, `password`, `real_name`, `status`)
VALUES (
    'ADMIN',
    'admin',
    '2cb9e6e09a928927147cc51e4c8467fc',
    '系统管理员',
    1
);

-- 给 admin 分配 O5 超级管理员等级
INSERT INTO `sys_employee_level`(`emp_id`, `level`)
SELECT id, 'O5' FROM `sys_employee` WHERE username='admin';

-- ================================================================
-- 结束
-- ================================================================
