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
-- 业务模块表（与各 Python 模块 INIT_SQL_LIST 保持一致）
-- 各模块启动时会自动 CREATE TABLE IF NOT EXISTS，此处集中列出便于手动建库
-- ================================================================

-- ---------- 客户数据管理（customer.py：节点 68 / 69 / 70） ----------
CREATE TABLE IF NOT EXISTS `biz_customer` (
    `id`           BIGINT       NOT NULL AUTO_INCREMENT COMMENT '客户ID',
    `cust_no`      VARCHAR(32)  NOT NULL COMMENT '客户编号(业务唯一)',
    `cust_type`    VARCHAR(16)  NOT NULL COMMENT 'personal个人 / org单位',
    `name`         VARCHAR(128) NOT NULL COMMENT '姓名或单位名称',
    `id_card`      VARCHAR(32)  DEFAULT NULL COMMENT '身份证号(个人)',
    `org_code`     VARCHAR(64)  DEFAULT NULL COMMENT '统一社会信用代码(单位)',
    `contact_person` VARCHAR(64) DEFAULT NULL COMMENT '联系人(单位)',
    `phone`        VARCHAR(20)  DEFAULT NULL COMMENT '手机号',
    `email`        VARCHAR(128) DEFAULT NULL COMMENT '邮箱',
    `password`     VARCHAR(128) DEFAULT NULL COMMENT '登录密码(加盐MD5, 可空)',
    `level`        VARCHAR(8)   DEFAULT NULL COMMENT '客户等级(如VIP/普通)',
    `status`       TINYINT      NOT NULL DEFAULT 1 COMMENT '1启用 0停用',
    `remark`       VARCHAR(255) DEFAULT NULL COMMENT '备注',
    `create_time`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `update_time`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_cust_no` (`cust_no`),
    KEY `idx_cust_type` (`cust_type`),
    KEY `idx_phone` (`phone`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='客户档案表';

CREATE TABLE IF NOT EXISTS `biz_customer_address` (
    `id`          BIGINT       NOT NULL AUTO_INCREMENT,
    `customer_id` BIGINT       NOT NULL COMMENT '所属客户ID',
    `recipient`   VARCHAR(64)  NOT NULL COMMENT '收件人',
    `phone`       VARCHAR(20)  DEFAULT NULL COMMENT '收件人电话',
    `province`    VARCHAR(64)  DEFAULT NULL COMMENT '省',
    `city`        VARCHAR(64)  DEFAULT NULL COMMENT '市',
    `district`    VARCHAR(64)  DEFAULT NULL COMMENT '区/县',
    `detail`      VARCHAR(255) NOT NULL COMMENT '详细地址',
    `is_default`  TINYINT      NOT NULL DEFAULT 0 COMMENT '1默认地址 0否',
    `create_time` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_customer` (`customer_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='客户地址表';

CREATE TABLE IF NOT EXISTS `biz_customer_tag` (
    `id`         BIGINT       NOT NULL AUTO_INCREMENT,
    `customer_id` BIGINT      NOT NULL COMMENT '所属客户ID',
    `tag_name`   VARCHAR(64)  NOT NULL COMMENT '标签名(偏好/订阅需求)',
    `tag_value`  VARCHAR(128) DEFAULT NULL COMMENT '标签值',
    `create_time` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_cust_tag` (`customer_id`, `tag_name`),
    KEY `idx_customer` (`customer_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='客户标签表';

-- ---------- 报刊数据管理（newspaper.py：节点 60 / 62 / 63 / 64） ----------
CREATE TABLE IF NOT EXISTS `biz_category` (
    `id`          BIGINT       NOT NULL AUTO_INCREMENT COMMENT '分类ID',
    `name`        VARCHAR(64)  NOT NULL COMMENT '分类名',
    `parent_id`   BIGINT       NOT NULL DEFAULT 0 COMMENT '父分类ID，0=顶级',
    `sort`        INT          NOT NULL DEFAULT 0 COMMENT '排序',
    `create_time` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_parent` (`parent_id`),
    KEY `idx_name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='报刊分类表';

CREATE TABLE IF NOT EXISTS `biz_newspaper` (
    `id`            BIGINT       NOT NULL AUTO_INCREMENT COMMENT '报刊ID',
    `paper_no`      VARCHAR(32)  NOT NULL COMMENT '报刊编号，业务唯一',
    `name`          VARCHAR(128) NOT NULL COMMENT '报刊名称',
    `cn_code`       VARCHAR(32)  DEFAULT NULL COMMENT 'CN号',
    `category_id`   BIGINT       DEFAULT NULL COMMENT '分类ID',
    `publish_cycle` VARCHAR(16)  DEFAULT NULL COMMENT 'daily/weekly/monthly/quarterly',
    `unit_price`    DECIMAL(10,2) NOT NULL DEFAULT 0 COMMENT '单期价格',
    `period_price`  DECIMAL(10,2) DEFAULT NULL COMMENT '整订周期价',
    `discount`      DECIMAL(5,2) NOT NULL DEFAULT 1.00 COMMENT '折扣系数，范围0-1',
    `publisher`     VARCHAR(128) DEFAULT NULL COMMENT '出版单位',
    `status`        TINYINT      NOT NULL DEFAULT 1 COMMENT '1=在售，0=停刊',
    `remark`        VARCHAR(255) DEFAULT NULL COMMENT '备注',
    `create_time`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `update_time`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_paper_no` (`paper_no`),
    KEY `idx_category` (`category_id`),
    KEY `idx_name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='报刊信息表';

-- ---------- 订阅管理（subscription.py：节点 76 / 77 / 78 / 79） ----------
CREATE TABLE IF NOT EXISTS `biz_subscription` (
    `id`           BIGINT        NOT NULL AUTO_INCREMENT COMMENT '订阅ID',
    `sub_no`       VARCHAR(32)   NOT NULL COMMENT '订阅单号',
    `customer_id`  BIGINT        NOT NULL COMMENT '客户ID',
    `newspaper_id` BIGINT        NOT NULL COMMENT '报刊ID',
    `address_id`   BIGINT        DEFAULT NULL COMMENT '投递地址ID',
    `start_date`   DATE          NOT NULL COMMENT '起订日期',
    `end_date`     DATE          NOT NULL COMMENT '终止日期',
    `periods`      INT           NOT NULL DEFAULT 0 COMMENT '期数',
    `unit_price`   DECIMAL(10,2) NOT NULL DEFAULT 0 COMMENT '下单时单价',
    `discount`     DECIMAL(5,2)  NOT NULL DEFAULT 1.00 COMMENT '折扣',
    `amount`       DECIMAL(10,2) NOT NULL DEFAULT 0 COMMENT '应付总额',
    `status`       VARCHAR(16)   NOT NULL DEFAULT 'active' COMMENT 'active有效/cancelled退订/changed换订/expired到期',
    `remark`       VARCHAR(255)  DEFAULT NULL COMMENT '备注',
    `create_time`  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `update_time`  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_sub_no` (`sub_no`),
    KEY `idx_customer` (`customer_id`),
    KEY `idx_newspaper` (`newspaper_id`),
    KEY `idx_status` (`status`),
    KEY `idx_end_date` (`end_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订阅表';

-- ---------- 报刊入库管理（stock.py：节点 84 / 85 / 86 / 87） ----------
CREATE TABLE IF NOT EXISTS `biz_stock` (
    `id`           BIGINT      NOT NULL AUTO_INCREMENT COMMENT '库存ID',
    `newspaper_id` BIGINT      NOT NULL COMMENT '报刊ID',
    `issue_no`     VARCHAR(32) NOT NULL COMMENT '期号',
    `quantity`     INT         NOT NULL DEFAULT 0 COMMENT '当前库存',
    `threshold`    INT         NOT NULL DEFAULT 0 COMMENT '预警阈值',
    `update_time`  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_paper_issue` (`newspaper_id`, `issue_no`),
    KEY `idx_paper` (`newspaper_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='库存快照表';

CREATE TABLE IF NOT EXISTS `biz_stock_in` (
    `id`            BIGINT      NOT NULL AUTO_INCREMENT COMMENT '入库流水ID',
    `newspaper_id`  BIGINT      NOT NULL COMMENT '报刊ID',
    `issue_no`      VARCHAR(32) NOT NULL COMMENT '期号',
    `quantity`      INT         NOT NULL COMMENT '本次入库数量',
    `operator_id`   BIGINT      DEFAULT NULL COMMENT '操作人ID',
    `operator_name` VARCHAR(64) DEFAULT NULL COMMENT '操作人姓名',
    `create_time`   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_paper` (`newspaper_id`),
    KEY `idx_time` (`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='入库流水表';

CREATE TABLE IF NOT EXISTS `biz_stock_check` (
    `id`            BIGINT       NOT NULL AUTO_INCREMENT COMMENT '盘点记录ID',
    `newspaper_id`  BIGINT       NOT NULL COMMENT '报刊ID',
    `issue_no`      VARCHAR(32)  NOT NULL COMMENT '期号',
    `system_qty`    INT          NOT NULL COMMENT '系统库存',
    `actual_qty`    INT          NOT NULL COMMENT '实盘库存',
    `diff`          INT          NOT NULL COMMENT '差异=实盘-系统',
    `remark`        VARCHAR(255) DEFAULT NULL COMMENT '备注',
    `operator_id`   BIGINT       DEFAULT NULL COMMENT '操作人ID',
    `operator_name` VARCHAR(64)  DEFAULT NULL COMMENT '操作人姓名',
    `create_time`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_paper` (`newspaper_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='库存盘点记录表';

-- ---------- 报刊发放系统（delivery.py：节点 92 / 93 / 94 / 95） ----------
CREATE TABLE IF NOT EXISTS `biz_delivery_task` (
    `id`              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '任务ID',
    `task_no`         VARCHAR(40)  NOT NULL COMMENT '任务单号(业务唯一)',
    `subscription_id` BIGINT       DEFAULT NULL COMMENT '订阅ID',
    `customer_id`     BIGINT       NOT NULL COMMENT '客户ID',
    `newspaper_id`    BIGINT       NOT NULL COMMENT '报刊ID',
    `address_id`      BIGINT       DEFAULT NULL COMMENT '投递地址ID',
    `district`        VARCHAR(64)  DEFAULT NULL COMMENT '区域(冗余便于分配)',
    `courier_id`      BIGINT       DEFAULT NULL COMMENT '派送员(sys_employee.id)',
    `courier_name`    VARCHAR(64)  DEFAULT NULL COMMENT '派送员名称',
    `deliver_date`    DATE         NOT NULL COMMENT '发放日期',
    `status`          VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending待派送/assigned已分配/signed已签收/abnormal异常/missing缺刊',
    `sign_time`       DATETIME     DEFAULT NULL COMMENT '签收时间',
    `remark`          VARCHAR(255) DEFAULT NULL COMMENT '备注(异常/缺刊原因)',
    `create_time`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_task_no` (`task_no`),
    UNIQUE KEY `uk_sub_date` (`subscription_id`, `deliver_date`),
    KEY `idx_courier` (`courier_id`),
    KEY `idx_date` (`deliver_date`),
    KEY `idx_status` (`status`),
    KEY `idx_customer` (`customer_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='报刊发放任务表';

CREATE TABLE IF NOT EXISTS `biz_missing` (
    `id`              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '缺刊记录ID',
    `task_id`         BIGINT       NOT NULL COMMENT '原发放任务ID',
    `newspaper_id`    BIGINT       NOT NULL COMMENT '报刊ID',
    `customer_id`     BIGINT       NOT NULL COMMENT '客户ID',
    `reason`          VARCHAR(255) DEFAULT NULL COMMENT '缺刊原因',
    `reissue_task_id` BIGINT       DEFAULT NULL COMMENT '补发任务ID',
    `status`          VARCHAR(16)  NOT NULL DEFAULT 'open' COMMENT 'open待处理/reissued已补发/closed关闭',
    `operator_id`     BIGINT       DEFAULT NULL COMMENT '处理人员ID',
    `operator_name`   VARCHAR(64)  DEFAULT NULL COMMENT '处理人员名称',
    `create_time`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    KEY `idx_task` (`task_id`),
    KEY `idx_status` (`status`),
    KEY `idx_customer` (`customer_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='缺刊处理表';

-- ================================================================
-- 结束
-- ================================================================
