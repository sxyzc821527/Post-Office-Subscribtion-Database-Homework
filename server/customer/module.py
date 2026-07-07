# -*- coding: utf-8 -*-
"""
================================================================
客户数据管理模块（业务模块）
================================================================
对应流程图节点：
    · 节点 40  客户数据管理
    · 节点 68  客户档案（个人 / 单位 客户信息管理）
    · 节点 69  地址管理（一个客户支持多个投递地址）
    · 节点 70  客户标签（标记客户偏好、订阅需求）
    · 节点 71  订阅历史（查看客户历史订阅记录）

技术栈：Python + pymysql + MySQL
设计原则：
    · 前后端分离，本文件为后端核心逻辑，提供可被 Web 层调用的 API 函数。
    · 复用 master/authority.py 中的 get_conn() 与 OperationLogService，
      避免重复造轮子，统一数据库连接入口与审计口径。
    · 订阅历史采用「防御性查询」：订阅管理模块（节点 42）尚未实现，
      若订阅表不存在则捕获异常返回空列表，待该模块建表后自动有数据。
================================================================
"""

import logging
import os
import sys
from typing import Any, Dict, List, Optional

import pymysql
from pymysql.cursors import DictCursor

# ----------------------------------------------------------------
# 复用权限模块的数据库连接 / 操作日志
# ----------------------------------------------------------------
from server.core.authority import DB_CONFIG, OperationLogService, get_conn, hash_password  # noqa: E402

# ----------------------------------------------------------------
# 日志
# ----------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("customer")

# 客户类型枚举
CUSTOMER_TYPE_PERSONAL = "personal"   # 个人客户
CUSTOMER_TYPE_ORG = "org"             # 单位客户
CUSTOMER_TYPES = (CUSTOMER_TYPE_PERSONAL, CUSTOMER_TYPE_ORG)

# 客户状态：1 启用 / 0 停用
STATUS_ACTIVE = 1
STATUS_INACTIVE = 0


# ================================================================
# 一、建表 DDL（对应节点 68 / 69 / 70）
# ================================================================
INIT_SQL_LIST: List[str] = [
    # ---------- 客户档案表（节点 68） ----------
    """
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
        `update_time`  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                     ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (`id`),
        UNIQUE KEY `uk_cust_no` (`cust_no`),
        KEY `idx_cust_type` (`cust_type`),
        KEY `idx_phone` (`phone`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='客户档案表';
    """,
    # ---------- 客户地址表（节点 69：支持多地址） ----------
    """
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
    """,
    # ---------- 客户标签表（节点 70） ----------
    """
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
    """,
]


def init_tables() -> None:
    """初始化客户相关表结构（幂等，已存在则跳过）。"""
    with get_conn() as conn:
        cur = conn.cursor()
        for sql in INIT_SQL_LIST:
            cur.execute(sql)
        conn.commit()
    logger.info("客户数据管理表结构初始化完成（biz_customer / 地址 / 标签）。")


# ================================================================
# 二、客户档案管理（节点 68）
# ================================================================
class CustomerService:
    """客户档案增删改查、起停、密码重置。"""

    # ---------- 增 ----------
    @staticmethod
    def add_customer(
        cust_no: str,
        cust_type: str,
        name: str,
        id_card: Optional[str] = None,
        org_code: Optional[str] = None,
        contact_person: Optional[str] = None,
        phone: Optional[str] = None,
        email: Optional[str] = None,
        password: Optional[str] = None,
        level: Optional[str] = None,
        remark: Optional[str] = None,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> int:
        """
        新增客户档案，返回新客户 ID。
        cust_type 必须为 personal / org。
        """
        if cust_type not in CUSTOMER_TYPES:
            raise ValueError(f"非法客户类型: {cust_type}（应为 {CUSTOMER_TYPES}）")

        hashed = hash_password(password) if password else None
        sql = (
            "INSERT INTO biz_customer"
            "(cust_no, cust_type, name, id_card, org_code, contact_person, "
            " phone, email, password, level, remark) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        )
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, (
                cust_no, cust_type, name, id_card, org_code, contact_person,
                phone, email, hashed, level, remark,
            ))
            conn.commit()
            new_id = cur.lastrowid

        OperationLogService.record(
            operator_id, operator_name, "客户档案", "新增",
            detail={"customer_id": new_id, "cust_no": cust_no, "name": name},
        )
        logger.info("新增客户 id=%s cust_no=%s name=%s", new_id, cust_no, name)
        return new_id

    # ---------- 删 ----------
    @staticmethod
    def delete_customer(customer_id: int,
                        operator_id: Optional[int] = None,
                        operator_name: Optional[str] = None) -> int:
        """
        删除客户（同时清理其地址、标签），返回受影响行数。
        注意：若该客户已有订阅记录，建议改为「停用」而非物理删除。
        """
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM biz_customer WHERE id=%s", (customer_id,))
            affected = cur.rowcount
            cur.execute(
                "DELETE FROM biz_customer_address WHERE customer_id=%s",
                (customer_id,),
            )
            cur.execute(
                "DELETE FROM biz_customer_tag WHERE customer_id=%s",
                (customer_id,),
            )
            conn.commit()

        if affected:
            OperationLogService.record(
                operator_id, operator_name, "客户档案", "删除",
                detail={"customer_id": customer_id},
            )
            logger.info("删除客户 id=%s affected=%s", customer_id, affected)
        return affected

    # ---------- 改 ----------
    @staticmethod
    def update_customer(customer_id: int, operator_id: Optional[int] = None,
                        operator_name: Optional[str] = None,
                        **fields) -> int:
        """
        修改客户档案，仅更新传入字段。
        支持字段：cust_type, name, id_card, org_code, contact_person,
                  phone, email, level, status, remark
        """
        allowed = {
            "cust_type", "name", "id_card", "org_code", "contact_person",
            "phone", "email", "level", "status", "remark",
        }
        if "cust_type" in fields and fields["cust_type"] not in CUSTOMER_TYPES:
            raise ValueError(
                f"非法客户类型: {fields['cust_type']}（应为 {CUSTOMER_TYPES}）")

        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return 0
        set_clause = ", ".join(f"{k}=%s" for k in updates)
        params = list(updates.values()) + [customer_id]
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"UPDATE biz_customer SET {set_clause} WHERE id=%s", params)
            conn.commit()
            affected = cur.rowcount

        OperationLogService.record(
            operator_id, operator_name, "客户档案", "修改",
            detail={"customer_id": customer_id, "fields": list(updates.keys())},
        )
        return affected

    @staticmethod
    def set_status(customer_id: int, status: int,
                   operator_id: Optional[int] = None,
                   operator_name: Optional[str] = None) -> int:
        """启用(1)/停用(0) 客户。"""
        return CustomerService.update_customer(
            customer_id, operator_id, operator_name, status=status)

    @staticmethod
    def reset_password(customer_id: int, new_password: str,
                       operator_id: Optional[int] = None,
                       operator_name: Optional[str] = None) -> int:
        """重置客户登录密码。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE biz_customer SET password=%s WHERE id=%s",
                (hash_password(new_password), customer_id),
            )
            conn.commit()
            affected = cur.rowcount
        OperationLogService.record(
            operator_id, operator_name, "客户档案", "重置密码",
            detail={"customer_id": customer_id},
        )
        return affected

    # ---------- 查 ----------
    @staticmethod
    def get_customer(customer_id: int) -> Optional[Dict[str, Any]]:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, cust_no, cust_type, name, id_card, org_code, "
                "contact_person, phone, email, level, status, remark, "
                "create_time, update_time "
                "FROM biz_customer WHERE id=%s",
                (customer_id,),
            )
            return cur.fetchone()

    @staticmethod
    def get_by_cust_no(cust_no: str) -> Optional[Dict[str, Any]]:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM biz_customer WHERE cust_no=%s", (cust_no,))
            return cur.fetchone()

    @staticmethod
    def list_customers(
        keyword: str = "",
        cust_type: Optional[str] = None,
        status: Optional[int] = None,
        level: Optional[str] = None,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """分页查询客户列表，支持按名称/编号/手机号模糊检索。"""
        where: List[str] = []
        params: List[Any] = []
        if keyword:
            where.append("(name LIKE %s OR cust_no LIKE %s OR phone LIKE %s)")
            kw = f"%{keyword}%"
            params.extend([kw, kw, kw])
        if cust_type:
            where.append("cust_type=%s")
            params.append(cust_type)
        if status is not None:
            where.append("status=%s")
            params.append(status)
        if level:
            where.append("level=%s")
            params.append(level)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        offset = (page - 1) * size

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT COUNT(*) AS total FROM biz_customer {where_sql}", params)
            total = cur.fetchone()["total"]
            cur.execute(
                f"SELECT id, cust_no, cust_type, name, phone, level, status, "
                f"create_time FROM biz_customer {where_sql} "
                f"ORDER BY id DESC LIMIT %s OFFSET %s",
                params + [size, offset],
            )
            rows = cur.fetchall()
        return {"total": total, "page": page, "size": size, "list": rows}


# ================================================================
# 三、客户地址管理（节点 69：支持多地址）
# ================================================================
class CustomerAddressService:
    """客户多地址增删改查、设置默认地址。"""

    @staticmethod
    def add_address(
        customer_id: int,
        recipient: str,
        detail: str,
        phone: Optional[str] = None,
        province: Optional[str] = None,
        city: Optional[str] = None,
        district: Optional[str] = None,
        is_default: bool = False,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> int:
        """新增客户地址。若 is_default=True，会先清掉该客户原默认地址。"""
        with get_conn() as conn:
            cur = conn.cursor()
            if is_default:
                cur.execute(
                    "UPDATE biz_customer_address SET is_default=0 "
                    "WHERE customer_id=%s",
                    (customer_id,),
                )
            cur.execute(
                "INSERT INTO biz_customer_address"
                "(customer_id, recipient, phone, province, city, district, "
                " detail, is_default) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (customer_id, recipient, phone, province, city, district,
                 detail, 1 if is_default else 0),
            )
            conn.commit()
            new_id = cur.lastrowid
        OperationLogService.record(
            operator_id, operator_name, "客户地址", "新增",
            detail={"address_id": new_id, "customer_id": customer_id},
        )
        return new_id

    @staticmethod
    def update_address(address_id: int, operator_id: Optional[int] = None,
                       operator_name: Optional[str] = None,
                       **fields) -> int:
        """修改地址，仅更新传入字段。"""
        allowed = {
            "recipient", "phone", "province", "city", "district", "detail",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return 0
        set_clause = ", ".join(f"{k}=%s" for k in updates)
        params = list(updates.values()) + [address_id]
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"UPDATE biz_customer_address SET {set_clause} WHERE id=%s",
                params)
            conn.commit()
            affected = cur.rowcount
        OperationLogService.record(
            operator_id, operator_name, "客户地址", "修改",
            detail={"address_id": address_id, "fields": list(updates.keys())},
        )
        return affected

    @staticmethod
    def delete_address(address_id: int,
                       operator_id: Optional[int] = None,
                       operator_name: Optional[str] = None) -> int:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM biz_customer_address WHERE id=%s", (address_id,))
            conn.commit()
            affected = cur.rowcount
        if affected:
            OperationLogService.record(
                operator_id, operator_name, "客户地址", "删除",
                detail={"address_id": address_id},
            )
        return affected

    @staticmethod
    def set_default(address_id: int, customer_id: int,
                    operator_id: Optional[int] = None,
                    operator_name: Optional[str] = None) -> int:
        """把某地址设为默认：先清该客户所有默认，再置目标为默认。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE biz_customer_address SET is_default=0 "
                "WHERE customer_id=%s",
                (customer_id,),
            )
            cur.execute(
                "UPDATE biz_customer_address SET is_default=1 "
                "WHERE id=%s AND customer_id=%s",
                (address_id, customer_id),
            )
            conn.commit()
            affected = cur.rowcount
        OperationLogService.record(
            operator_id, operator_name, "客户地址", "设为默认",
            detail={"address_id": address_id, "customer_id": customer_id},
        )
        return affected

    @staticmethod
    def list_addresses(customer_id: int) -> List[Dict[str, Any]]:
        """列出某客户的全部地址，默认地址排在最前。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, customer_id, recipient, phone, province, city, "
                "district, detail, is_default, create_time "
                "FROM biz_customer_address WHERE customer_id=%s "
                "ORDER BY is_default DESC, id ASC",
                (customer_id,),
            )
            return cur.fetchall()

    @staticmethod
    def get_default_address(customer_id: int) -> Optional[Dict[str, Any]]:
        """获取客户默认地址（无则返回 None）。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM biz_customer_address "
                "WHERE customer_id=%s AND is_default=1 LIMIT 1",
                (customer_id,),
            )
            return cur.fetchone()


# ================================================================
# 四、客户标签管理（节点 70）
# ================================================================
class CustomerTagService:
    """客户标签：标记偏好、订阅需求。同一客户 + 标签名唯一。"""

    @staticmethod
    def set_tag(
        customer_id: int,
        tag_name: str,
        tag_value: Optional[str] = None,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> int:
        """
        给客户打标签（存在则更新 tag_value）。
        tag_name 示例：偏好（财经/体育/少儿）、订阅需求（季付/送上门）。
        """
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO biz_customer_tag(customer_id, tag_name, tag_value) "
                "VALUES(%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE tag_value=VALUES(tag_value)",
                (customer_id, tag_name, tag_value),
            )
            conn.commit()
            affected = cur.rowcount
        OperationLogService.record(
            operator_id, operator_name, "客户标签", "设置",
            detail={"customer_id": customer_id, "tag_name": tag_name,
                    "tag_value": tag_value},
        )
        return affected

    @staticmethod
    def remove_tag(customer_id: int, tag_name: str,
                   operator_id: Optional[int] = None,
                   operator_name: Optional[str] = None) -> int:
        """移除客户的某个标签。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM biz_customer_tag "
                "WHERE customer_id=%s AND tag_name=%s",
                (customer_id, tag_name),
            )
            conn.commit()
            affected = cur.rowcount
        if affected:
            OperationLogService.record(
                operator_id, operator_name, "客户标签", "移除",
                detail={"customer_id": customer_id, "tag_name": tag_name},
            )
        return affected

    @staticmethod
    def list_tags(customer_id: int) -> List[Dict[str, Any]]:
        """列出某客户全部标签。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, customer_id, tag_name, tag_value, create_time "
                "FROM biz_customer_tag WHERE customer_id=%s ORDER BY id ASC",
                (customer_id,),
            )
            return cur.fetchall()

    @staticmethod
    def find_customers_by_tag(tag_name: str,
                              tag_value: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        反向检索：按标签找客户（用于精准营销 / 推荐）。
        传 tag_value 时进一步精确匹配标签值。
        """
        sql = (
            "SELECT c.id, c.cust_no, c.name, c.phone, c.level, c.status, "
            "t.tag_name, t.tag_value "
            "FROM biz_customer_tag t "
            "INNER JOIN biz_customer c ON c.id = t.customer_id "
            "WHERE t.tag_name=%s"
        )
        params: List[Any] = [tag_name]
        if tag_value:
            sql += " AND t.tag_value=%s"
            params.append(tag_value)
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(sql + " ORDER BY c.id ASC", params)
            return cur.fetchall()


# ================================================================
# 五、订阅历史（节点 71：防御性查询）
# ================================================================
# 说明：订阅管理模块（节点 42）尚未实现，订阅表暂不存在。
# 这里采用「防御性查询」——表不存在时捕获异常返回空列表，
# 待订阅管理模块建好 biz_subscription 表后，本接口自动有数据返回。
_SUBSCRIPTION_TABLE = "biz_subscription"


class SubscriptionHistoryService:
    """查看客户历史订阅记录（依赖订阅管理模块的 biz_subscription 表）。"""

    @staticmethod
    def _table_exists(cur) -> bool:
        """检测订阅表是否已存在。"""
        cur.execute(
            "SELECT COUNT(*) AS c FROM information_schema.tables "
            "WHERE table_schema=%s AND table_name=%s",
            (DB_CONFIG.get("database"), _SUBSCRIPTION_TABLE),
        )
        return cur.fetchone()["c"] > 0

    @staticmethod
    def list_by_customer(
        customer_id: int,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """
        分页查询某客户的订阅历史。
        若订阅表尚未建立（订阅管理模块未完成），返回空结果而非报错。
        """
        offset = (page - 1) * size
        with get_conn() as conn:
            cur = conn.cursor()
            if not SubscriptionHistoryService._table_exists(cur):
                logger.warning(
                    "订阅表 %s 尚未创建（订阅管理模块未实现），"
                    "订阅历史返回空。", _SUBSCRIPTION_TABLE)
                return {"total": 0, "page": page, "size": size, "list": []}

            # 订阅表存在：按客户维度查询历史
            # 字段做容错（若订阅管理模块字段不同，需届时对齐）
            cur.execute(
                f"SELECT COUNT(*) AS total FROM {_SUBSCRIPTION_TABLE} "
                f"WHERE customer_id=%s",
                (customer_id,),
            )
            total = cur.fetchone()["total"]
            cur.execute(
                f"SELECT * FROM {_SUBSCRIPTION_TABLE} "
                f"WHERE customer_id=%s ORDER BY id DESC LIMIT %s OFFSET %s",
                (customer_id, size, offset),
            )
            rows = cur.fetchall()
        return {"total": total, "page": page, "size": size, "list": rows}

    @staticmethod
    def summary(customer_id: int) -> Dict[str, Any]:
        """
        客户订阅汇总（订阅数、最近一次订阅时间等）。
        订阅表不存在时返回零值摘要。
        """
        with get_conn() as conn:
            cur = conn.cursor()
            if not SubscriptionHistoryService._table_exists(cur):
                return {"customer_id": customer_id, "total_orders": 0,
                        "latest_create_time": None}
            cur.execute(
                f"SELECT COUNT(*) AS total_orders, "
                f"MAX(create_time) AS latest_create_time "
                f"FROM {_SUBSCRIPTION_TABLE} WHERE customer_id=%s",
                (customer_id,),
            )
            row = cur.fetchone() or {}
        return {
            "customer_id": customer_id,
            "total_orders": row.get("total_orders", 0),
            "latest_create_time": row.get("latest_create_time"),
        }


# ================================================================
# 六、对外统一入口（供 Web 层 / 命令行测试调用）
# ================================================================
def main() -> None:
    """命令行自测：建表 + 打印各 Service 提示。"""
    init_tables()
    print("[OK] 客户数据管理模块已就绪：")
    print("  - CustomerService            客户档案 CRUD/起停/重置密码")
    print("  - CustomerAddressService     多地址 CRUD/默认地址")
    print("  - CustomerTagService         标签设置/移除/反向检索")
    print("  - SubscriptionHistoryService 订阅历史(防御性查询)")


if __name__ == "__main__":
    main()
