# -*- coding: utf-8 -*-
"""
================================================================
订阅管理模块（业务模块）
================================================================
对应流程图节点：
    · 节点 42  订阅管理
    · 节点 76  新建订阅（选客户、选报刊、选起止日期、自动算价）
    · 节点 77  退订/换订（退订结算、报刊替换）
    · 节点 78  续订提醒（到期前自动提醒）
    · 节点 79  订阅统计（按报刊、时段、客户类型多维统计）

技术栈：Python + pymysql + MySQL
设计原则：
    · 前后端分离，本文件为后端核心逻辑，提供可被 Web 层调用的 API 函数。
    · 复用 master/authority.py 中的 get_conn() 与 OperationLogService，
      避免重复造轮子，统一数据库连接入口与审计口径。
    · 订阅单号自动生成（SUB+时间戳+随机数保证唯一）。
    · 价格计算自动从报刊表读取单价和折扣。
    · 退订结算按剩余天数比例退款。
================================================================
"""

import logging
import os
import random
import sys
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pymysql
from pymysql.cursors import DictCursor

# ----------------------------------------------------------------
# 复用权限模块的数据库连接 / 操作日志
# ----------------------------------------------------------------
from server.core.authority import DB_CONFIG, OperationLogService, get_conn  # noqa: E402

# ----------------------------------------------------------------
# 日志
# ----------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("subscription")

# 订阅状态常量
STATUS_ACTIVE = "active"          # 有效订阅
STATUS_CANCELLED = "cancelled"    # 已退订
STATUS_CHANGED = "changed"        # 已换订
STATUS_EXPIRED = "expired"        # 已到期


# ================================================================
# 一、建表 DDL（节点 76/77/78/79）
# ================================================================
INIT_SQL_LIST: List[str] = [
    # ---------- 订阅表（节点 76/77） ----------
    """
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
        `status`       VARCHAR(16)   NOT NULL DEFAULT 'active'
                       COMMENT 'active有效/cancelled退订/changed换订/expired到期',
        `remark`       VARCHAR(255)  DEFAULT NULL COMMENT '备注',
        `create_time`  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
        `update_time`  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP
                       ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (`id`),
        UNIQUE KEY `uk_sub_no` (`sub_no`),
        KEY `idx_customer` (`customer_id`),
        KEY `idx_newspaper` (`newspaper_id`),
        KEY `idx_status` (`status`),
        KEY `idx_end_date` (`end_date`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订阅表';
    """,
]


def init_tables() -> None:
    """初始化订阅表结构（幂等，已存在则跳过）。"""
    with get_conn() as conn:
        cur = conn.cursor()
        for sql in INIT_SQL_LIST:
            cur.execute(sql)
        conn.commit()
    logger.info("订阅管理表结构初始化完成（biz_subscription）。")


# ================================================================
# 二、订阅服务工具函数
# ================================================================
def gen_sub_no() -> str:
    """
    生成唯一的订阅单号。
    格式：SUB+时间戳（10位）+随机数（6位）。
    """
    timestamp = str(int(time.time()))[-10:]          # 最后10位时间戳
    rand_part = str(random.randint(100000, 999999))  # 6位随机数
    return f"SUB{timestamp}{rand_part}"


def calc_amount(unit_price: Decimal, periods: int, discount: Decimal) -> Decimal:
    """计算订阅总金额：amount = round(unit_price * periods * discount, 2)。"""
    result = unit_price * Decimal(periods) * discount
    return result.quantize(Decimal("0.01"))


# ================================================================
# 三、订阅管理（节点 76 新建订阅 / 节点 77 退订/换订）
# ================================================================
class SubscriptionService:
    """订阅增删改查、新建订阅、退订、换订。"""

    # ---------- 增（新建订阅） ----------
    @staticmethod
    def create_subscription(
        customer_id: int,
        newspaper_id: int,
        start_date: date,
        end_date: date,
        periods: int,
        address_id: Optional[int] = None,
        remark: Optional[str] = None,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> int:
        """
        新建订阅（节点 76）。
        校验客户存在、报刊存在且在售，从报刊表读单价折扣自动算金额，
        生成唯一订阅单号，插入并记日志，返回新订阅 ID。
        """
        # 校验客户与报刊
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM biz_customer WHERE id=%s", (customer_id,))
            if not cur.fetchone():
                raise ValueError(f"客户 id={customer_id} 不存在")

            cur.execute(
                "SELECT id, unit_price, discount, status FROM biz_newspaper "
                "WHERE id=%s",
                (newspaper_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"报刊 id={newspaper_id} 不存在")
            if row.get("status") == 0:  # status=0 表示停刊
                raise ValueError(f"报刊 id={newspaper_id} 已停刊")

            unit_price = Decimal(str(row.get("unit_price", 0)))
            discount = Decimal(str(row.get("discount", 1)))

        # 计算应付金额并生成订阅单号
        amount = calc_amount(unit_price, periods, discount)
        sub_no = gen_sub_no()

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO biz_subscription"
                "(sub_no, customer_id, newspaper_id, address_id, "
                " start_date, end_date, periods, unit_price, discount, amount, remark) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (sub_no, customer_id, newspaper_id, address_id,
                 start_date, end_date, periods, unit_price, discount, amount, remark),
            )
            conn.commit()
            new_id = cur.lastrowid

        OperationLogService.record(
            operator_id, operator_name, "订阅管理", "新建订阅",
            detail={
                "subscription_id": new_id, "sub_no": sub_no,
                "customer_id": customer_id, "newspaper_id": newspaper_id,
                "periods": periods, "amount": str(amount),
            },
        )
        logger.info(
            "新建订阅 id=%s sub_no=%s customer_id=%s newspaper_id=%s amount=%s",
            new_id, sub_no, customer_id, newspaper_id, amount)
        return new_id

    # ---------- 查 ----------
    @staticmethod
    def get_subscription(sub_id: int) -> Optional[Dict[str, Any]]:
        """查询单个订阅详情。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, sub_no, customer_id, newspaper_id, address_id, "
                "start_date, end_date, periods, unit_price, discount, amount, "
                "status, remark, create_time, update_time "
                "FROM biz_subscription WHERE id=%s",
                (sub_id,),
            )
            return cur.fetchone()

    @staticmethod
    def list_subscriptions(
        customer_id: Optional[int] = None,
        newspaper_id: Optional[int] = None,
        status: Optional[str] = None,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """分页查询订阅列表，支持按客户 / 报刊 / 状态过滤。"""
        where: List[str] = []
        params: List[Any] = []
        if customer_id:
            where.append("customer_id=%s")
            params.append(customer_id)
        if newspaper_id:
            where.append("newspaper_id=%s")
            params.append(newspaper_id)
        if status:
            where.append("status=%s")
            params.append(status)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        offset = (page - 1) * size

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT COUNT(*) AS total FROM biz_subscription {where_sql}",
                params)
            total = cur.fetchone()["total"]
            cur.execute(
                f"SELECT id, sub_no, customer_id, newspaper_id, status, "
                f"start_date, end_date, periods, amount, create_time "
                f"FROM biz_subscription {where_sql} "
                f"ORDER BY id DESC LIMIT %s OFFSET %s",
                params + [size, offset],
            )
            rows = cur.fetchall()
        return {"total": total, "page": page, "size": size, "list": rows}

    # ---------- 改（退订） ----------
    @staticmethod
    def cancel_subscription(
        sub_id: int,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        退订处理（节点 77 退订结算）。
        校验订阅存在且为 active，按剩余天数比例计算退款，置状态为 cancelled。
        返回 {"affected": 受影响行数, "refund": 退款金额}。
        """
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, customer_id, start_date, end_date, amount, status "
                "FROM biz_subscription WHERE id=%s",
                (sub_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"订阅 id={sub_id} 不存在")
            if row["status"] != STATUS_ACTIVE:
                raise ValueError(
                    f"订阅 id={sub_id} 状态为 {row['status']}，只有 active 状态才能退订")

            today = date.today()
            start_date = row["start_date"]
            end_date = row["end_date"]

            # 按剩余天数比例退款
            total_days = (end_date - start_date).days + 1
            remaining_days = (end_date - today).days + 1
            if remaining_days <= 0 or total_days <= 0:
                refund = Decimal("0")
            else:
                refund_ratio = Decimal(remaining_days) / Decimal(total_days)
                original_amount = Decimal(str(row["amount"]))
                refund = (original_amount * refund_ratio).quantize(Decimal("0.01"))

            cur.execute(
                "UPDATE biz_subscription SET status=%s WHERE id=%s",
                (STATUS_CANCELLED, sub_id),
            )
            conn.commit()
            affected = cur.rowcount

        OperationLogService.record(
            operator_id, operator_name, "订阅管理", "退订",
            detail={"subscription_id": sub_id,
                    "customer_id": row["customer_id"], "refund": str(refund)},
        )
        logger.info("退订 id=%s customer_id=%s refund=%s affected=%s",
                    sub_id, row["customer_id"], refund, affected)
        return {"affected": affected, "refund": float(refund)}

    # ---------- 改（换订） ----------
    @staticmethod
    def change_subscription(
        sub_id: int,
        new_newspaper_id: int,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        换订处理（节点 77 换订）。
        原订阅置为 changed，用原客户/日期/期数按新报刊价格新建 active 订阅。
        返回 {"old_id": ..., "new_id": ...}。
        """
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, customer_id, start_date, end_date, periods, "
                "address_id, status FROM biz_subscription WHERE id=%s",
                (sub_id,),
            )
            old_row = cur.fetchone()
            if not old_row:
                raise ValueError(f"订阅 id={sub_id} 不存在")
            if old_row["status"] != STATUS_ACTIVE:
                raise ValueError(
                    f"订阅 id={sub_id} 状态为 {old_row['status']}，只有 active 状态才能换订")

        # 用原客户和日期按新报刊新建订阅
        new_id = SubscriptionService.create_subscription(
            customer_id=old_row["customer_id"],
            newspaper_id=new_newspaper_id,
            start_date=old_row["start_date"],
            end_date=old_row["end_date"],
            periods=old_row["periods"],
            address_id=old_row["address_id"],
            remark=f"换订自订阅ID {sub_id}",
            operator_id=operator_id,
            operator_name=operator_name,
        )

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE biz_subscription SET status=%s WHERE id=%s",
                (STATUS_CHANGED, sub_id),
            )
            conn.commit()

        OperationLogService.record(
            operator_id, operator_name, "订阅管理", "换订",
            detail={"old_subscription_id": sub_id, "new_subscription_id": new_id,
                    "customer_id": old_row["customer_id"],
                    "new_newspaper_id": new_newspaper_id},
        )
        logger.info("换订 old_id=%s new_id=%s new_newspaper_id=%s",
                    sub_id, new_id, new_newspaper_id)
        return {"old_id": sub_id, "new_id": new_id}


# ================================================================
# 四、续订提醒（节点 78）
# ================================================================
class RenewalService:
    """续订提醒相关业务。"""

    @staticmethod
    def list_expiring(days: int = 7) -> List[Dict[str, Any]]:
        """
        查询即将到期的订阅（节点 78）。
        条件：end_date 在 [今天, 今天+days] 区间内，且 status='active'。
        联表带出客户名、报刊名用于提醒。
        """
        today = date.today()
        end_boundary = today + timedelta(days=days)
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT s.id, s.sub_no, s.customer_id, s.newspaper_id, "
                "s.end_date, c.name AS customer_name, c.phone AS customer_phone, "
                "n.name AS newspaper_name "
                "FROM biz_subscription s "
                "LEFT JOIN biz_customer c ON c.id = s.customer_id "
                "LEFT JOIN biz_newspaper n ON n.id = s.newspaper_id "
                "WHERE s.status=%s AND s.end_date BETWEEN %s AND %s "
                "ORDER BY s.end_date ASC, s.id ASC",
                (STATUS_ACTIVE, today, end_boundary),
            )
            rows = cur.fetchall()
        logger.info("查询 %d 天内即将到期订阅: 共 %d 条", days, len(rows) if rows else 0)
        return rows or []


# ================================================================
# 五、订阅统计（节点 79）
# ================================================================
class SubscriptionStatService:
    """多维度订阅统计：按报刊、时段、客户类型。"""

    @staticmethod
    def by_newspaper() -> List[Dict[str, Any]]:
        """按报刊统计订阅数与总金额。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT s.newspaper_id, n.name AS newspaper_name, "
                "COUNT(*) AS subscription_count, SUM(s.amount) AS total_amount "
                "FROM biz_subscription s "
                "LEFT JOIN biz_newspaper n ON n.id = s.newspaper_id "
                "GROUP BY s.newspaper_id, n.name "
                "ORDER BY total_amount DESC, subscription_count DESC",
            )
            rows = cur.fetchall()
        logger.info("按报刊统计完成: %d 种报刊", len(rows) if rows else 0)
        return rows or []

    @staticmethod
    def by_period(start: date, end: date) -> Dict[str, Any]:
        """按时间段统计 create_time 在 [start, end] 区间内的订阅数与总额。"""
        start_dt = datetime.combine(start, datetime.min.time())
        end_dt = datetime.combine(end, datetime.max.time())
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) AS subscription_count, SUM(amount) AS total_amount "
                "FROM biz_subscription WHERE create_time BETWEEN %s AND %s",
                (start_dt, end_dt),
            )
            row = cur.fetchone() or {}
        result = {
            "start": start, "end": end,
            "subscription_count": row.get("subscription_count", 0),
            "total_amount": float(row.get("total_amount") or 0),
        }
        logger.info("按时段统计 [%s, %s]: %d 订阅, 金额 %s",
                    start, end, result["subscription_count"], result["total_amount"])
        return result

    @staticmethod
    def by_customer_type() -> List[Dict[str, Any]]:
        """按客户类型（personal/org）统计订阅数与总额。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT c.cust_type, COUNT(s.id) AS subscription_count, "
                "SUM(s.amount) AS total_amount "
                "FROM biz_subscription s "
                "LEFT JOIN biz_customer c ON c.id = s.customer_id "
                "GROUP BY c.cust_type ORDER BY total_amount DESC",
            )
            rows = cur.fetchall()
        logger.info("按客户类型统计完成: %d 种类型", len(rows) if rows else 0)
        return rows or []


# ================================================================
# 六、对外统一入口（供 Web 层 / 命令行测试调用）
# ================================================================
def main() -> None:
    """命令行自测：建表 + 打印各 Service 提示。"""
    init_tables()
    print("[OK] 订阅管理模块已就绪：")
    print("  - SubscriptionService       订阅增删改查/新建/退订/换订")
    print("  - RenewalService            续订提醒（查询即将到期）")
    print("  - SubscriptionStatService   多维统计（按报刊/时段/客户类型）")


if __name__ == "__main__":
    main()
