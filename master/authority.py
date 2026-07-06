# -*- coding: utf-8 -*-
"""
================================================================
核心权限管理模块 —— 员工权限管理
================================================================
对应思路图节点：
    · 节点 10  员工权限管理（员工 CRUD、员工权限）
    · 节点 17  标准员工权限分配（可自定义其它权限）
    · 节点 20-25  权限等级 O5 ~ O0
    · 节点 51-55  权限分级制度（路由/按钮/接口/数据 四类权限）
    · 节点 101 权限管理（创建权限等级、分配权限）
    · 节点 103 操作日志（全系统操作追查审计）

技术栈：Python + pymysql + MySQL
设计原则：前后端分离，本文件为后端核心逻辑，提供可被 Web 层调用的 API 函数。
================================================================
"""

import functools
import hashlib
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pymysql
from pymysql.cursors import DictCursor

# ----------------------------------------------------------------
# 日志配置
# ----------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("authority")

# ----------------------------------------------------------------
# 数据库连接配置（实际部署时改为读取配置文件 / 环境变量）
# ----------------------------------------------------------------
DB_CONFIG: Dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "root",
    "database": "post_office",
    "charset": "utf8mb4",
    "cursorclass": DictCursor,
}


def get_conn():
    """获取数据库连接，统一入口，便于切换连接池。"""
    return pymysql.connect(**DB_CONFIG)


# ================================================================
# 一、权限等级定义（O5 ~ O0）
# ================================================================
# 标准权限等级：数字越小权限范围越聚焦（O5 最高，O0 最末）。
# 每个等级关联一组"菜单/按钮/接口/数据"权限标识，可自定义扩展。
STANDARD_LEVELS: List[Dict[str, Any]] = [
    {
        "level": "O5",
        "name": "超级管理员",
        "desc": "拥有系统全部权限",
        "permissions": ["*"],  # 通配符，代表全部权限
    },
    {
        "level": "O4",
        "name": "报刊数据管理员",
        "desc": "管理报刊数据、分类",
        "permissions": [
            "menu:newspaper", "menu:category",
            "btn:newspaper:add", "btn:newspaper:edit", "btn:newspaper:del",
            "api:newspaper:*", "api:category:*",
            "data:newspaper:all",
        ],
    },
    {
        "level": "O3",
        "name": "客户/订阅管理员",
        "desc": "管理客户、处理订阅",
        "permissions": [
            "menu:customer", "menu:subscription",
            "btn:customer:*", "btn:subscription:*",
            "api:customer:*", "api:subscription:*",
            "data:customer:all",
        ],
    },
    {
        "level": "O2",
        "name": "入库管理员",
        "desc": "报刊入库、库存盘点",
        "permissions": [
            "menu:stock", "menu:inventory",
            "btn:stock:in", "btn:stock:check",
            "api:stock:*", "api:inventory:*",
            "data:stock:all",
        ],
    },
    {
        "level": "O1",
        "name": "发放员",
        "desc": "报刊发放、签收确认",
        "permissions": [
            "menu:delivery", "menu:sign",
            "btn:delivery:assign", "btn:delivery:confirm",
            "api:delivery:*", "api:sign:*",
            "data:delivery:self",  # 仅看自己负责的任务
        ],
    },
    {
        "level": "O0",
        "name": "财务对账员",
        "desc": "订阅费用统计、对账",
        "permissions": [
            "menu:finance", "menu:report",
            "btn:finance:stat", "btn:finance:reconcile",
            "api:finance:*", "api:report:*",
            "data:finance:all",
        ],
    },
]


# ================================================================
# 二、数据库表初始化（DDL）
# ================================================================
INIT_SQL_LIST: List[str] = [
    # 员工表
    """
    CREATE TABLE IF NOT EXISTS `sys_employee` (
        `id`            BIGINT       NOT NULL AUTO_INCREMENT COMMENT '员工ID',
        `emp_no`        VARCHAR(32)  NOT NULL COMMENT '工号',
        `username`      VARCHAR(64)  NOT NULL COMMENT '登录名',
        `password`      VARCHAR(128) NOT NULL COMMENT '密码(加盐MD5)',
        `real_name`     VARCHAR(64)  DEFAULT NULL COMMENT '真实姓名',
        `phone`         VARCHAR(20)  DEFAULT NULL COMMENT '手机号',
        `status`        TINYINT      NOT NULL DEFAULT 1 COMMENT '1启用 0停用',
        `create_time`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        `update_time`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                    ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (`id`),
        UNIQUE KEY `uk_username` (`username`),
        UNIQUE KEY `uk_emp_no` (`emp_no`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='员工表';
    """,
    # 权限等级表
    """
    CREATE TABLE IF NOT EXISTS `sys_auth_level` (
        `level`   VARCHAR(8)  NOT NULL COMMENT '等级编码 O5~O0',
        `name`    VARCHAR(64) NOT NULL COMMENT '等级名称',
        `desc`    VARCHAR(255) DEFAULT NULL COMMENT '描述',
        PRIMARY KEY (`level`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='权限等级表';
    """,
    # 权限点表（菜单/按钮/接口/数据 四类）
    """
    CREATE TABLE IF NOT EXISTS `sys_permission` (
        `id`     BIGINT       NOT NULL AUTO_INCREMENT,
        `code`   VARCHAR(128) NOT NULL COMMENT '权限标识 如 menu:newspaper',
        `type`   VARCHAR(16)  NOT NULL COMMENT 'menu/btn/api/data',
        `name`   VARCHAR(128) DEFAULT NULL COMMENT '权限名称',
        PRIMARY KEY (`id`),
        UNIQUE KEY `uk_code` (`code`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='权限点表';
    """,
    # 员工-等级 分配表
    """
    CREATE TABLE IF NOT EXISTS `sys_employee_level` (
        `emp_id`    BIGINT      NOT NULL,
        `level`     VARCHAR(8)  NOT NULL,
        `assign_time` DATETIME  NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (`emp_id`, `level`),
        KEY `idx_level` (`level`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='员工-等级分配表';
    """,
    # 员工-自定义权限 表（在等级之外额外授予/撤销权限）
    """
    CREATE TABLE IF NOT EXISTS `sys_employee_permission` (
        `emp_id`    BIGINT      NOT NULL,
        `perm_code` VARCHAR(128) NOT NULL,
        `granted`   TINYINT     NOT NULL DEFAULT 1 COMMENT '1授予 0撤销',
        PRIMARY KEY (`emp_id`, `perm_code`),
        KEY `idx_emp` (`emp_id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='员工自定义权限表';
    """,
    # 操作日志表
    """
    CREATE TABLE IF NOT EXISTS `sys_operation_log` (
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
    """,
]


def init_database() -> None:
    """初始化所有表结构，并写入标准权限等级。"""
    with get_conn() as conn:
        cur = conn.cursor()
        for sql in INIT_SQL_LIST:
            cur.execute(sql)

        # 写入标准权限等级（忽略已存在）
        for lv in STANDARD_LEVELS:
            cur.execute(
                "INSERT IGNORE INTO `sys_auth_level`(level, name, `desc`) "
                "VALUES(%s, %s, %s)",
                (lv["level"], lv["name"], lv["desc"]),
            )
        logger.info("数据库表结构与标准权限等级初始化完成。")


# ================================================================
# 三、密码加密工具
# ================================================================
_SALT = "post_office_2026"


def hash_password(plain: str) -> str:
    """加盐 MD5 加密。"""
    return hashlib.md5(f"{plain}{_SALT}".encode("utf-8")).hexdigest()


def verify_password(plain: str, hashed: str) -> bool:
    return hash_password(plain) == hashed


# ================================================================
# 四、员工 CRUD（节点 10）
# ================================================================
class EmployeeService:
    """员工增删改查、起停、密码重置。"""

    # ---------- 增 ----------
    @staticmethod
    def add_employee(emp_no: str, username: str, password: str,
                     real_name: Optional[str] = None,
                     phone: Optional[str] = None) -> int:
        """新增员工，返回新员工 ID。"""
        sql = (
            "INSERT INTO sys_employee(emp_no, username, password, real_name, phone) "
            "VALUES(%s, %s, %s, %s, %s)"
        )
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, (emp_no, username, hash_password(password),
                              real_name, phone))
            conn.commit()
            new_id = cur.lastrowid
            logger.info("新增员工 id=%s username=%s", new_id, username)
            return new_id

    # ---------- 删 ----------
    @staticmethod
    def delete_employee(emp_id: int) -> int:
        """删除员工（同时清理其权限分配），返回受影响行数。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM sys_employee WHERE id=%s", (emp_id,))
            cur.execute("DELETE FROM sys_employee_level WHERE emp_id=%s", (emp_id,))
            cur.execute("DELETE FROM sys_employee_permission WHERE emp_id=%s",
                        (emp_id,))
            conn.commit()
            affected = cur.rowcount
            logger.info("删除员工 id=%s affected=%s", emp_id, affected)
            return affected

    # ---------- 改 ----------
    @staticmethod
    def update_employee(emp_id: int, **fields) -> int:
        """
        修改员工信息，仅更新传入的字段。
        支持字段：real_name, phone, status
        """
        allowed = {"real_name", "phone", "status"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return 0
        set_clause = ", ".join(f"{k}=%s" for k in updates)
        params = list(updates.values()) + [emp_id]
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"UPDATE sys_employee SET {set_clause} WHERE id=%s", params)
            conn.commit()
            return cur.rowcount

    @staticmethod
    def set_status(emp_id: int, status: int) -> int:
        """启用(1)/停用(0) 员工。"""
        return EmployeeService.update_employee(emp_id, status=status)

    @staticmethod
    def reset_password(emp_id: int, new_password: str) -> int:
        """重置员工密码。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE sys_employee SET password=%s WHERE id=%s",
                (hash_password(new_password), emp_id),
            )
            conn.commit()
            return cur.rowcount

    # ---------- 查 ----------
    @staticmethod
    def get_employee(emp_id: int) -> Optional[Dict[str, Any]]:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, emp_no, username, real_name, phone, status, "
                "create_time, update_time FROM sys_employee WHERE id=%s",
                (emp_id,),
            )
            return cur.fetchone()

    @staticmethod
    def get_employee_by_username(username: str) -> Optional[Dict[str, Any]]:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM sys_employee WHERE username=%s", (username,))
            return cur.fetchone()

    @staticmethod
    def list_employees(keyword: str = "", status: Optional[int] = None,
                       page: int = 1, size: int = 20) -> Dict[str, Any]:
        """分页查询员工列表。"""
        where = []
        params: List[Any] = []
        if keyword:
            where.append("(username LIKE %s OR real_name LIKE %s OR emp_no LIKE %s)")
            kw = f"%{keyword}%"
            params.extend([kw, kw, kw])
        if status is not None:
            where.append("status=%s")
            params.append(status)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        offset = (page - 1) * size
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) AS total FROM sys_employee {where_sql}",
                        params)
            total = cur.fetchone()["total"]

            cur.execute(
                f"SELECT id, emp_no, username, real_name, phone, status, "
                f"create_time FROM sys_employee {where_sql} "
                f"ORDER BY id DESC LIMIT %s OFFSET %s",
                params + [size, offset],
            )
            rows = cur.fetchall()
        return {"total": total, "page": page, "size": size, "list": rows}


# ================================================================
# 五、权限等级与权限分配（节点 17 / 101）
# ================================================================
class PermissionService:
    """权限等级管理 + 员工权限分配。"""

    # ---------- 权限等级 ----------
    @staticmethod
    def list_levels() -> List[Dict[str, Any]]:
        """列出全部权限等级。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT level, name, `desc` FROM sys_auth_level ORDER BY level")
            return cur.fetchall()

    @staticmethod
    def create_level(level: str, name: str, desc: str = "") -> int:
        """创建自定义权限等级。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO sys_auth_level(level, name, `desc`) VALUES(%s,%s,%s)",
                (level, name, desc),
            )
            conn.commit()
            return cur.lastrowid

    # ---------- 员工-等级分配 ----------
    @staticmethod
    def assign_level(emp_id: int, level: str) -> int:
        """给员工分配权限等级（一个员工可拥有多个等级，取并集）。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT IGNORE INTO sys_employee_level(emp_id, level) VALUES(%s,%s)",
                (emp_id, level),
            )
            conn.commit()
            return cur.rowcount

    @staticmethod
    def revoke_level(emp_id: int, level: str) -> int:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM sys_employee_level WHERE emp_id=%s AND level=%s",
                (emp_id, level),
            )
            conn.commit()
            return cur.rowcount

    @staticmethod
    def get_employee_levels(emp_id: int) -> List[str]:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT level FROM sys_employee_level WHERE emp_id=%s", (emp_id,)
            )
            return [r["level"] for r in cur.fetchall()]

    # ---------- 员工自定义权限（在等级之外的细粒度调整） ----------
    @staticmethod
    def grant_permission(emp_id: int, perm_code: str) -> int:
        """额外授予权限点。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO sys_employee_permission(emp_id, perm_code, granted) "
                "VALUES(%s,%s,1) ON DUPLICATE KEY UPDATE granted=1",
                (emp_id, perm_code),
            )
            conn.commit()
            return cur.rowcount

    @staticmethod
    def revoke_permission(emp_id: int, perm_code: str) -> int:
        """撤销权限点（granted=0）。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO sys_employee_permission(emp_id, perm_code, granted) "
                "VALUES(%s,%s,0) ON DUPLICATE KEY UPDATE granted=0",
                (emp_id, perm_code),
            )
            conn.commit()
            return cur.rowcount

    # ---------- 计算员工最终权限集合 ----------
    @staticmethod
    def get_employee_permissions(emp_id: int) -> List[str]:
        """
        计算员工最终拥有的权限点列表。
        规则：等级权限的并集 + 自定义授予 - 自定义撤销。
        O5（超级管理员）直接返回 ['*'] 通配符。
        """
        levels = PermissionService.get_employee_levels(emp_id)

        # 超级管理员直接全部权限
        if "O5" in levels:
            return ["*"]

        # 收集等级带来的权限
        perm_set: set = set()
        for lv in levels:
            for std in STANDARD_LEVELS:
                if std["level"] == lv:
                    perm_set.update(std["permissions"])

        # 叠加自定义权限
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT perm_code, granted FROM sys_employee_permission WHERE emp_id=%s",
                (emp_id,),
            )
            for row in cur.fetchall():
                if row["granted"]:
                    perm_set.add(row["perm_code"])
                else:
                    perm_set.discard(row["perm_code"])
        return sorted(perm_set)


# ================================================================
# 六、权限校验（节点 51-55：四种权限）
# ================================================================
def _has_permission(emp_id: int, required: str) -> bool:
    """
    判断员工是否拥有某权限点。
    支持通配符匹配，例如：
        required='menu:newspaper' 可被 'menu:*' 或 '*' 命中
    """
    perms = PermissionService.get_employee_permissions(emp_id)
    if "*" in perms:
        return True
    # 精确匹配
    if required in perms:
        return True
    # 通配符匹配（menu:* 命中 menu:xxx）
    for p in perms:
        if p.endswith(":*") and required.startswith(p[:-1]):
            return True
    return False


def require_permission(required: str):
    """
    接口权限校验装饰器（节点 54）。
    用法：
        @require_permission("api:newspaper:add")
        def add_newspaper(emp_id, ...): ...
    被装饰函数的第一个参数必须是 emp_id。
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(emp_id: int, *args, **kwargs):
            if not _has_permission(emp_id, required):
                logger.warning("越权访问: emp_id=%s 需要 %s", emp_id, required)
                raise PermissionError(f"无权限: {required}")
            return func(emp_id, *args, **kwargs)

        return wrapper

    return decorator


class PermissionChecker:
    """供 Web 层调用的权限检查工具，对应四类权限。"""

    @staticmethod
    def check_menu(emp_id: int, menu_code: str) -> bool:
        """路由/菜单权限（节点 52）：控制用户能看见什么页面。"""
        return _has_permission(emp_id, f"menu:{menu_code}")

    @staticmethod
    def check_button(emp_id: int, btn_code: str) -> bool:
        """按钮权限（节点 53）：控制用户能看见什么按钮。"""
        return _has_permission(emp_id, f"btn:{btn_code}")

    @staticmethod
    def check_api(emp_id: int, api_code: str) -> bool:
        """接口权限（节点 54）：后端 API 校验，防止越权。"""
        return _has_permission(emp_id, f"api:{api_code}")

    @staticmethod
    def check_data(emp_id: int, data_code: str) -> bool:
        """数据权限（节点 55）：控制用户能看见哪些数据。"""
        return _has_permission(emp_id, f"data:{data_code}")

    @staticmethod
    def visible_menus(emp_id: int, all_menus: List[str]) -> List[str]:
        """根据员工权限过滤可见菜单列表。"""
        return [m for m in all_menus if PermissionChecker.check_menu(emp_id, m)]


# ================================================================
# 七、操作日志（节点 103）
# ================================================================
class OperationLogService:
    """全系统操作追查审计。"""

    @staticmethod
    def record(emp_id: Optional[int], emp_name: Optional[str],
               module: str, action: str,
               detail: Any = None, ip: Optional[str] = None) -> int:
        """记录一条操作日志。"""
        detail_str = json.dumps(detail, ensure_ascii=False) if detail else None
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO sys_operation_log"
                "(emp_id, emp_name, module, action, detail, ip) "
                "VALUES(%s,%s,%s,%s,%s,%s)",
                (emp_id, emp_name, module, action, detail_str, ip),
            )
            conn.commit()
            return cur.lastrowid

    @staticmethod
    def list_logs(emp_id: Optional[int] = None, module: Optional[str] = None,
                  start: Optional[str] = None, end: Optional[str] = None,
                  page: int = 1, size: int = 50) -> Dict[str, Any]:
        """分页查询操作日志。"""
        where, params = [], []
        if emp_id is not None:
            where.append("emp_id=%s")
            params.append(emp_id)
        if module:
            where.append("module=%s")
            params.append(module)
        if start:
            where.append("create_time>=%s")
            params.append(start)
        if end:
            where.append("create_time<=%s")
            params.append(end)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        offset = (page - 1) * size
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT COUNT(*) AS total FROM sys_operation_log {where_sql}", params
            )
            total = cur.fetchone()["total"]
            cur.execute(
                f"SELECT * FROM sys_operation_log {where_sql} "
                f"ORDER BY id DESC LIMIT %s OFFSET %s",
                params + [size, offset],
            )
            rows = cur.fetchall()
        return {"total": total, "page": page, "size": size, "list": rows}


def log_operation(module: str, action: str):
    """
    操作日志装饰器：自动记录被装饰函数的调用。
    被装饰函数的第一个参数必须是 emp_id。
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(emp_id: int, *args, **kwargs):
            emp = EmployeeService.get_employee(emp_id)
            emp_name = emp["real_name"] or emp["username"] if emp else None
            try:
                result = func(emp_id, *args, **kwargs)
                OperationLogService.record(
                    emp_id, emp_name, module, action,
                    detail={"args": str(args)[:500]},
                )
                return result
            except Exception as e:
                OperationLogService.record(
                    emp_id, emp_name, module, action,
                    detail={"error": str(e)},
                )
                raise

        return wrapper

    return decorator


# ================================================================
# 八、使用示例 / 自测
# ================================================================
def _demo():
    """演示用法（需先 init_database 并确保 MySQL 可连）。"""
    try:
        init_database()
    except Exception as e:
        logger.error("数据库初始化失败，请检查 MySQL 连接: %s", e)
        return

    # 1. 新增员工
    eid = EmployeeService.add_employee(
        emp_no="E001", username="zhangsan",
        password="123456", real_name="张三", phone="13800000000",
    )
    # 2. 分配权限等级
    PermissionService.assign_level(eid, "O4")
    # 3. 额外授予一个权限
    PermissionService.grant_permission(eid, "menu:delivery")
    # 4. 查看最终权限
    perms = PermissionService.get_employee_permissions(eid)
    print(f"员工 {eid} 的权限: {perms}")
    # 5. 权限校验
    print("能看报刊菜单?", PermissionChecker.check_menu(eid, "newspaper"))
    print("能看发放菜单?", PermissionChecker.check_menu(eid, "delivery"))
    # 6. 操作日志
    OperationLogService.record(eid, "张三", "员工管理", "新增员工",
                               detail={"emp_no": "E001"})


if __name__ == "__main__":
    _demo()
