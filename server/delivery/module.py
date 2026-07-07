# -*- coding: utf-8 -*-
"""
================================================================
报刊发放管理模块（业务模块）
================================================================
对应流程图节点：
    · 节点 46  报刊发放系统（总节点）
    · 节点 92  发放任务生成（订单 -> 每日发放任务）
    · 节点 93  派送员分配（按区域自动分配任务给派送员）
    · 节点 94  签收确认（派送员确认签收 / 异常上报）
    · 节点 95  缺刊处理（缺刊登记、补发订单）

技术栈：Python + pymysql + MySQL
设计原则：
    · 前后端分离，本文件为后端核心逻辑，提供 Web 层调用的 API 函数。
    · 复用 master/authority.py 中的 get_conn() 与 OperationLogService。
    · 发放任务使用业务唯一键(subscription_id, deliver_date)实现幂等，
      避免重复生成任务；task_no 格式 DLV+YYYYMMDD+订阅ID保证唯一。
    · 支持多派送员并行配送、异常上报、缺刊处理与补发链路。
================================================================
"""

import logging
import os
import random
import string
import sys
from datetime import date
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
logger = logging.getLogger("delivery")

# 发放任务状态：pending/assigned/signed/abnormal/missing
STATUS_PENDING = "pending"            # 待派送
STATUS_ASSIGNED = "assigned"          # 已分配
STATUS_SIGNED = "signed"              # 已签收
STATUS_ABNORMAL = "abnormal"          # 异常
STATUS_MISSING = "missing"            # 缺刊

# 缺刊记录状态：open/reissued/closed
MISSING_STATUS_OPEN = "open"          # 待处理
MISSING_STATUS_REISSUED = "reissued"  # 已补发
MISSING_STATUS_CLOSED = "closed"      # 关闭


# ================================================================
# 一、建表 DDL（对应节点 92 / 93 / 94 / 95）
# ================================================================
INIT_SQL_LIST: List[str] = [
    # ---------- 发放任务表（节点 92/93/94） ----------
    """
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
        `status`          VARCHAR(16)  NOT NULL DEFAULT 'pending'
                          COMMENT 'pending待派送/assigned已分配/signed已签收/abnormal异常/missing缺刊',
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
    """,
    # ---------- 缺刊处理表（节点 95） ----------
    """
    CREATE TABLE IF NOT EXISTS `biz_missing` (
        `id`              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '缺刊记录ID',
        `task_id`         BIGINT       NOT NULL COMMENT '原发放任务ID',
        `newspaper_id`    BIGINT       NOT NULL COMMENT '报刊ID',
        `customer_id`     BIGINT       NOT NULL COMMENT '客户ID',
        `reason`          VARCHAR(255) DEFAULT NULL COMMENT '缺刊原因',
        `reissue_task_id` BIGINT       DEFAULT NULL COMMENT '补发任务ID',
        `status`          VARCHAR(16)  NOT NULL DEFAULT 'open'
                          COMMENT 'open待处理/reissued已补发/closed关闭',
        `operator_id`     BIGINT       DEFAULT NULL COMMENT '处理人员ID',
        `operator_name`   VARCHAR(64)  DEFAULT NULL COMMENT '处理人员名称',
        `create_time`     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (`id`),
        KEY `idx_task` (`task_id`),
        KEY `idx_status` (`status`),
        KEY `idx_customer` (`customer_id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='缺刊处理表';
    """,
]


def init_tables() -> None:
    """初始化报刊发放相关表结构（幂等，已存在则跳过）。"""
    with get_conn() as conn:
        cur = conn.cursor()
        for sql in INIT_SQL_LIST:
            cur.execute(sql)
        conn.commit()
    logger.info("报刊发放管理表结构初始化完成（biz_delivery_task / biz_missing）。")


# ================================================================
# 二、发放任务管理（节点 92 / 93 / 94）
# ================================================================
class DeliveryTaskService:
    """发放任务管理：生成日常配送任务、分配派送员、签收、异常上报。"""

    @staticmethod
    def _generate_task_no(subscription_id: Optional[int] = None) -> str:
        """
        生成唯一任务号：DLV + YYYYMMDD + subscription_id(若有) + 随机码。
        确保任意时间、任意订阅的任务号全局唯一。
        """
        today = date.today().strftime("%Y%m%d")
        suffix = f"{subscription_id:08d}" if subscription_id else ""
        random_part = "".join(
            random.choices(string.ascii_uppercase + string.digits, k=6))
        return f"DLV{today}{suffix}{random_part}"

    @staticmethod
    def generate_daily_tasks(
        deliver_date: date,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        节点 92：生成每日发放任务。
        扫所有 status='active' 且 start_date<=deliver_date<=end_date 的订阅，
        为其生成一条发放任务（INSERT IGNORE + uk_sub_date 幂等）。
        订阅无指定地址时取客户默认地址，district 冗余存地址区域。
        返回 {"deliver_date":..,"created":新建数,"skipped":已存在数}。
        """
        created_count = 0
        skipped_count = 0

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT s.id, s.customer_id, s.newspaper_id, s.address_id, "
                "ca.district "
                "FROM biz_subscription s "
                "LEFT JOIN biz_customer_address ca ON ca.id = s.address_id "
                "WHERE s.status='active' "
                "AND s.start_date <= %s AND s.end_date >= %s "
                "ORDER BY s.id ASC",
                (deliver_date, deliver_date),
            )
            subscriptions = cur.fetchall()

            for sub in subscriptions:
                sub_id = sub["id"]
                cust_id = sub["customer_id"]
                newspaper_id = sub["newspaper_id"]
                address_id = sub["address_id"]
                district = sub["district"]

                # 订阅无地址时取客户默认地址
                if not address_id:
                    cur.execute(
                        "SELECT id, district FROM biz_customer_address "
                        "WHERE customer_id=%s AND is_default=1 LIMIT 1",
                        (cust_id,),
                    )
                    default_addr = cur.fetchone()
                    if default_addr:
                        address_id = default_addr["id"]
                        district = default_addr["district"]

                task_no = DeliveryTaskService._generate_task_no(sub_id)
                cur.execute(
                    "INSERT IGNORE INTO biz_delivery_task "
                    "(task_no, subscription_id, customer_id, newspaper_id, "
                    " address_id, district, deliver_date, status, create_time) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,NOW())",
                    (task_no, sub_id, cust_id, newspaper_id, address_id,
                     district, deliver_date, STATUS_PENDING),
                )
                if cur.rowcount > 0:
                    created_count += 1
                else:
                    skipped_count += 1

            conn.commit()

        if created_count > 0:
            OperationLogService.record(
                operator_id, operator_name, "发放任务", "每日生成",
                detail={"deliver_date": deliver_date.isoformat(),
                        "created": created_count, "skipped": skipped_count},
            )
            logger.info("生成日期 %s 的发放任务：新建=%s 已跳过=%s",
                        deliver_date, created_count, skipped_count)

        return {"deliver_date": deliver_date.isoformat(),
                "created": created_count, "skipped": skipped_count}

    @staticmethod
    def assign_by_district(
        deliver_date: date,
        mapping: Dict[str, int],
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> Dict[str, int]:
        """
        节点 93：按区域分配派送员。
        mapping = {区域名: sys_employee.id}；把该日 status='pending' 的任务
        按 district 匹配，置 courier_id / courier_name / status='assigned'。
        返回各区域分配数量统计。
        """
        stats: Dict[str, int] = {}
        with get_conn() as conn:
            cur = conn.cursor()
            for district_name, courier_id in mapping.items():
                cur.execute(
                    "SELECT real_name FROM sys_employee WHERE id=%s LIMIT 1",
                    (courier_id,),
                )
                emp = cur.fetchone()
                courier_name = emp["real_name"] if emp else None
                if not courier_name:
                    logger.warning("区域 %s 的派送员 id=%s 不存在",
                                   district_name, courier_id)
                    stats[district_name] = 0
                    continue

                cur.execute(
                    "UPDATE biz_delivery_task "
                    "SET courier_id=%s, courier_name=%s, status=%s "
                    "WHERE deliver_date=%s AND district=%s AND status=%s",
                    (courier_id, courier_name, STATUS_ASSIGNED,
                     deliver_date, district_name, STATUS_PENDING),
                )
                stats[district_name] = cur.rowcount
            conn.commit()

        OperationLogService.record(
            operator_id, operator_name, "发放任务", "按区域分配",
            detail={"deliver_date": deliver_date.isoformat(), "distribution": stats},
        )
        logger.info("按区域分配派送员完成(日期=%s)：%s", deliver_date, stats)
        return stats

    @staticmethod
    def list_tasks(
        deliver_date: Optional[date] = None,
        courier_id: Optional[int] = None,
        status: Optional[str] = None,
        customer_id: Optional[int] = None,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """分页查询发放任务，支持按日期 / 派送员 / 状态 / 客户筛选。"""
        where: List[str] = []
        params: List[Any] = []
        if deliver_date:
            where.append("deliver_date=%s")
            params.append(deliver_date)
        if courier_id:
            where.append("courier_id=%s")
            params.append(courier_id)
        if status:
            where.append("status=%s")
            params.append(status)
        if customer_id:
            where.append("customer_id=%s")
            params.append(customer_id)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        offset = (page - 1) * size

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT COUNT(*) AS total FROM biz_delivery_task {where_sql}",
                params)
            total = cur.fetchone()["total"]
            cur.execute(
                f"SELECT id, task_no, subscription_id, customer_id, newspaper_id, "
                f"address_id, district, courier_id, courier_name, deliver_date, "
                f"status, sign_time, remark, create_time "
                f"FROM biz_delivery_task {where_sql} "
                f"ORDER BY id DESC LIMIT %s OFFSET %s",
                params + [size, offset],
            )
            rows = cur.fetchall()
        return {"total": total, "page": page, "size": size, "list": rows}

    @staticmethod
    def sign(
        task_id: int,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> int:
        """
        节点 94 签收：派送员确认签收。
        status 必须为 assigned 或 pending，置 status='signed'、sign_time=NOW()。
        """
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, status FROM biz_delivery_task WHERE id=%s",
                (task_id,),
            )
            task = cur.fetchone()
            if not task:
                raise ValueError(f"发放任务 id={task_id} 不存在")
            if task["status"] not in (STATUS_ASSIGNED, STATUS_PENDING):
                raise ValueError(
                    f"任务状态非法：id={task_id} status={task['status']} "
                    f"（仅 assigned/pending 可签收）")
            cur.execute(
                "UPDATE biz_delivery_task SET status=%s, sign_time=NOW() WHERE id=%s",
                (STATUS_SIGNED, task_id),
            )
            conn.commit()

        OperationLogService.record(
            operator_id, operator_name, "发放任务", "签收",
            detail={"task_id": task_id},
        )
        logger.info("任务 id=%s 已签收", task_id)
        return 1

    @staticmethod
    def report_abnormal(
        task_id: int,
        remark: str,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> int:
        """节点 94 异常上报：置 status='abnormal'、记录 remark。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE biz_delivery_task SET status=%s, remark=%s WHERE id=%s",
                (STATUS_ABNORMAL, remark, task_id),
            )
            conn.commit()
            affected = cur.rowcount
        OperationLogService.record(
            operator_id, operator_name, "发放任务", "异常上报",
            detail={"task_id": task_id, "remark": remark},
        )
        logger.info("任务 id=%s 异常上报：%s", task_id, remark)
        return affected


# ================================================================
# 三、缺刊处理（节点 95）
# ================================================================
class MissingService:
    """缺刊管理：登记缺刊、查询、补发。"""

    @staticmethod
    def report_missing(
        task_id: int,
        reason: str,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> int:
        """
        节点 95 缺刊登记：读原任务拿 newspaper_id/customer_id，写 biz_missing
        （status='open'），把原任务 status 置 'missing'。返回缺刊记录 id。
        """
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, newspaper_id, customer_id FROM biz_delivery_task "
                "WHERE id=%s LIMIT 1",
                (task_id,),
            )
            task = cur.fetchone()
            if not task:
                raise ValueError(f"发放任务 id={task_id} 不存在")

            cur.execute(
                "INSERT INTO biz_missing "
                "(task_id, newspaper_id, customer_id, reason, status, "
                " operator_id, operator_name, create_time) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,NOW())",
                (task_id, task["newspaper_id"], task["customer_id"], reason,
                 MISSING_STATUS_OPEN, operator_id, operator_name),
            )
            missing_id = cur.lastrowid
            cur.execute(
                "UPDATE biz_delivery_task SET status=%s WHERE id=%s",
                (STATUS_MISSING, task_id),
            )
            conn.commit()

        OperationLogService.record(
            operator_id, operator_name, "缺刊处理", "登记缺刊",
            detail={"missing_id": missing_id, "task_id": task_id, "reason": reason},
        )
        logger.info("缺刊登记：missing_id=%s task_id=%s reason=%s",
                    missing_id, task_id, reason)
        return missing_id

    @staticmethod
    def reissue(
        missing_id: int,
        deliver_date: date,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        节点 95 补发：据缺刊记录新建一条发放任务（status='pending'），
        回填 biz_missing.reissue_task_id、status='reissued'。
        返回 {"missing_id":.., "reissue_task_id":..}。
        """
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT m.id, m.task_id, m.newspaper_id, m.customer_id, "
                "t.subscription_id, t.address_id, t.district "
                "FROM biz_missing m "
                "INNER JOIN biz_delivery_task t ON t.id = m.task_id "
                "WHERE m.id=%s LIMIT 1",
                (missing_id,),
            )
            missing = cur.fetchone()
            if not missing:
                raise ValueError(f"缺刊记录 id={missing_id} 不存在")

            subscription_id = missing["subscription_id"]
            task_no = DeliveryTaskService._generate_task_no(subscription_id)
            cur.execute(
                "INSERT INTO biz_delivery_task "
                "(task_no, subscription_id, customer_id, newspaper_id, "
                " address_id, district, deliver_date, status, create_time) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,NOW())",
                (task_no, subscription_id, missing["customer_id"],
                 missing["newspaper_id"], missing["address_id"],
                 missing["district"], deliver_date, STATUS_PENDING),
            )
            reissue_task_id = cur.lastrowid
            cur.execute(
                "UPDATE biz_missing SET reissue_task_id=%s, status=%s WHERE id=%s",
                (reissue_task_id, MISSING_STATUS_REISSUED, missing_id),
            )
            conn.commit()

        OperationLogService.record(
            operator_id, operator_name, "缺刊处理", "生成补发任务",
            detail={"missing_id": missing_id, "reissue_task_id": reissue_task_id,
                    "deliver_date": deliver_date.isoformat()},
        )
        logger.info("补发任务生成：missing_id=%s reissue_task_id=%s deliver_date=%s",
                    missing_id, reissue_task_id, deliver_date)
        return {"missing_id": missing_id, "reissue_task_id": reissue_task_id}

    @staticmethod
    def list_missing(
        status: Optional[str] = None,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """分页查询缺刊记录，支持按状态筛选。"""
        where: List[str] = []
        params: List[Any] = []
        if status:
            where.append("status=%s")
            params.append(status)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        offset = (page - 1) * size

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT COUNT(*) AS total FROM biz_missing {where_sql}", params)
            total = cur.fetchone()["total"]
            cur.execute(
                f"SELECT id, task_id, newspaper_id, customer_id, reason, "
                f"reissue_task_id, status, operator_id, operator_name, create_time "
                f"FROM biz_missing {where_sql} "
                f"ORDER BY id DESC LIMIT %s OFFSET %s",
                params + [size, offset],
            )
            rows = cur.fetchall()
        return {"total": total, "page": page, "size": size, "list": rows}


# ================================================================
# 四、对外统一入口（供 Web 层 / 命令行测试调用）
# ================================================================
def main() -> None:
    """命令行自测：建表 + 打印各 Service 提示。"""
    init_tables()
    print("[OK] 报刊发放管理模块已就绪：")
    print("  - DeliveryTaskService   发放任务生成/分配/签收/异常上报")
    print("  - MissingService        缺刊登记/补发/查询")


if __name__ == "__main__":
    main()
