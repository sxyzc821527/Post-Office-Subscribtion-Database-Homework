# -*- coding: utf-8 -*-
"""
================================================================
报刊入库管理模块（业务模块）
================================================================
对应流程图节点：
    · 节点 44  报刊入库管理
    · 节点 84  入库登记（按期号批量入库，自动更新库存）
    · 节点 85  库存盘点（定期盘点，差异调整）
    · 节点 86  库存预警（库存低于阈值自动告警）
    · 节点 87  入库查询（按报刊、日期范围查询入库流水）

技术栈：Python + pymysql + MySQL
设计原则：
    · 前后端分离，本文件为后端核心逻辑，提供可被 Web 层调用的 API 函数。
    · 复用 master/authority.py 中的 get_conn() 与 OperationLogService，
      统一数据库连接入口与审计口径。
    · 库存采用「快照 + 流水」双表：biz_stock 记当前库存，
      biz_stock_in 记每笔入库流水，biz_stock_check 记盘点差异。
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
from server.core.authority import DB_CONFIG, OperationLogService, get_conn  # noqa: E402

# ----------------------------------------------------------------
# 日志
# ----------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("stock")


# ================================================================
# 一、建表 DDL（对应节点 84 / 85 / 86 / 87）
# ================================================================
INIT_SQL_LIST: List[str] = [
    # ---------- 库存快照表（节点 84/86） ----------
    """
    CREATE TABLE IF NOT EXISTS `biz_stock` (
        `id`           BIGINT      NOT NULL AUTO_INCREMENT COMMENT '库存ID',
        `newspaper_id` BIGINT      NOT NULL COMMENT '报刊ID',
        `issue_no`     VARCHAR(32) NOT NULL COMMENT '期号',
        `quantity`     INT         NOT NULL DEFAULT 0 COMMENT '当前库存',
        `threshold`    INT         NOT NULL DEFAULT 0 COMMENT '预警阈值',
        `update_time`  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP
                                   ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (`id`),
        UNIQUE KEY `uk_paper_issue` (`newspaper_id`, `issue_no`),
        KEY `idx_paper` (`newspaper_id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='库存快照表';
    """,
    # ---------- 入库流水表（节点 84/87） ----------
    """
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
    """,
    # ---------- 盘点记录表（节点 85） ----------
    """
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
    """,
]


def init_tables() -> None:
    """初始化入库管理相关表结构（幂等，已存在则跳过）。"""
    with get_conn() as conn:
        cur = conn.cursor()
        for sql in INIT_SQL_LIST:
            cur.execute(sql)
        conn.commit()
    logger.info("报刊入库管理表结构初始化完成"
                "（biz_stock / biz_stock_in / biz_stock_check）。")


# ================================================================
# 二、入库登记与流水查询（节点 84 / 87）
# ================================================================
class StockInService:
    """入库登记、批量入库、入库流水查询。"""

    @staticmethod
    def stock_in(
        newspaper_id: int,
        issue_no: str,
        quantity: int,
        threshold: Optional[int] = None,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        入库登记（节点 84）。
        写一条入库流水，并累加更新库存快照（不存在则新建）。
        传 threshold 时一并更新预警阈值。
        返回 {"stock_in_id": 流水ID, "current_qty": 累加后库存}。
        """
        if quantity <= 0:
            raise ValueError(f"入库数量必须大于 0，当前为 {quantity}")

        with get_conn() as conn:
            cur = conn.cursor()
            # 1. 写入库流水
            cur.execute(
                "INSERT INTO biz_stock_in"
                "(newspaper_id, issue_no, quantity, operator_id, operator_name) "
                "VALUES(%s,%s,%s,%s,%s)",
                (newspaper_id, issue_no, quantity, operator_id, operator_name),
            )
            stock_in_id = cur.lastrowid

            # 2. 累加库存快照（存在则累加，不存在则新建）
            if threshold is not None:
                cur.execute(
                    "INSERT INTO biz_stock(newspaper_id, issue_no, quantity, threshold) "
                    "VALUES(%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE quantity=quantity+VALUES(quantity), "
                    "threshold=VALUES(threshold)",
                    (newspaper_id, issue_no, quantity, threshold),
                )
            else:
                cur.execute(
                    "INSERT INTO biz_stock(newspaper_id, issue_no, quantity) "
                    "VALUES(%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE quantity=quantity+VALUES(quantity)",
                    (newspaper_id, issue_no, quantity),
                )

            # 3. 读回累加后的库存
            cur.execute(
                "SELECT quantity FROM biz_stock "
                "WHERE newspaper_id=%s AND issue_no=%s",
                (newspaper_id, issue_no),
            )
            current_qty = cur.fetchone()["quantity"]
            conn.commit()

        OperationLogService.record(
            operator_id, operator_name, "报刊入库", "入库登记",
            detail={"newspaper_id": newspaper_id, "issue_no": issue_no,
                    "quantity": quantity, "current_qty": current_qty},
        )
        logger.info("入库登记 newspaper_id=%s issue_no=%s +%s -> %s",
                    newspaper_id, issue_no, quantity, current_qty)
        return {"stock_in_id": stock_in_id, "current_qty": current_qty}

    @staticmethod
    def batch_stock_in(
        rows: List[Dict[str, Any]],
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        批量入库（节点 84）。
        rows 每项含 newspaper_id / issue_no / quantity（可选 threshold）。
        逐条容错，返回 {"success": 成功数, "fail": [{"row_index","error"}]}。
        """
        success_count = 0
        fail_list: List[Dict[str, Any]] = []
        for idx, row in enumerate(rows):
            try:
                StockInService.stock_in(
                    newspaper_id=row["newspaper_id"],
                    issue_no=row["issue_no"],
                    quantity=int(row["quantity"]),
                    threshold=row.get("threshold"),
                    operator_id=operator_id,
                    operator_name=operator_name,
                )
                success_count += 1
            except Exception as e:
                fail_list.append({"row_index": idx, "error": str(e)})
                logger.warning("批量入库第 %d 行失败: %s", idx, str(e))

        OperationLogService.record(
            operator_id, operator_name, "报刊入库", "批量入库",
            detail={"success": success_count, "fail_count": len(fail_list)},
        )
        return {"success": success_count, "fail": fail_list}

    @staticmethod
    def list_stock_in(
        newspaper_id: Optional[int] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """
        入库流水查询（节点 87）：按报刊、日期范围查询。
        start / end 为日期时间字符串（如 '2026-01-01' / '2026-12-31 23:59:59'）。
        """
        where: List[str] = []
        params: List[Any] = []
        if newspaper_id is not None:
            where.append("si.newspaper_id=%s")
            params.append(newspaper_id)
        if start:
            where.append("si.create_time>=%s")
            params.append(start)
        if end:
            where.append("si.create_time<=%s")
            params.append(end)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        offset = (page - 1) * size

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT COUNT(*) AS total FROM biz_stock_in si {where_sql}",
                params)
            total = cur.fetchone()["total"]
            cur.execute(
                f"SELECT si.id, si.newspaper_id, n.name AS newspaper_name, "
                f"si.issue_no, si.quantity, si.operator_id, si.operator_name, "
                f"si.create_time "
                f"FROM biz_stock_in si "
                f"LEFT JOIN biz_newspaper n ON n.id = si.newspaper_id "
                f"{where_sql} ORDER BY si.id DESC LIMIT %s OFFSET %s",
                params + [size, offset],
            )
            rows = cur.fetchall()
        return {"total": total, "page": page, "size": size, "list": rows}


# ================================================================
# 三、库存查询与预警（节点 86）
# ================================================================
class StockService:
    """库存查询、预警、阈值设置。"""

    @staticmethod
    def get_stock(newspaper_id: int, issue_no: str) -> Optional[Dict[str, Any]]:
        """查询某报刊某期号的库存快照。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, newspaper_id, issue_no, quantity, threshold, update_time "
                "FROM biz_stock WHERE newspaper_id=%s AND issue_no=%s",
                (newspaper_id, issue_no),
            )
            return cur.fetchone()

    @staticmethod
    def list_stock(
        newspaper_id: Optional[int] = None,
        only_warning: bool = False,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """
        库存列表查询。
        only_warning=True 时只返回 quantity<=threshold 的预警库存。
        """
        where: List[str] = []
        params: List[Any] = []
        if newspaper_id is not None:
            where.append("s.newspaper_id=%s")
            params.append(newspaper_id)
        if only_warning:
            where.append("s.quantity<=s.threshold")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        offset = (page - 1) * size

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT COUNT(*) AS total FROM biz_stock s {where_sql}", params)
            total = cur.fetchone()["total"]
            cur.execute(
                f"SELECT s.id, s.newspaper_id, n.name AS newspaper_name, "
                f"s.issue_no, s.quantity, s.threshold, s.update_time "
                f"FROM biz_stock s "
                f"LEFT JOIN biz_newspaper n ON n.id = s.newspaper_id "
                f"{where_sql} ORDER BY s.newspaper_id ASC, s.issue_no ASC "
                f"LIMIT %s OFFSET %s",
                params + [size, offset],
            )
            rows = cur.fetchall()
        return {"total": total, "page": page, "size": size, "list": rows}

    @staticmethod
    def warning_list() -> List[Dict[str, Any]]:
        """库存预警（节点 86）：返回所有 quantity<=threshold 的库存（带报刊名）。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT s.id, s.newspaper_id, n.name AS newspaper_name, "
                "s.issue_no, s.quantity, s.threshold, s.update_time "
                "FROM biz_stock s "
                "LEFT JOIN biz_newspaper n ON n.id = s.newspaper_id "
                "WHERE s.quantity<=s.threshold "
                "ORDER BY (s.threshold - s.quantity) DESC",
            )
            return cur.fetchall()

    @staticmethod
    def set_threshold(
        newspaper_id: int,
        issue_no: str,
        threshold: int,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> int:
        """设置某报刊某期号的库存预警阈值。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO biz_stock(newspaper_id, issue_no, quantity, threshold) "
                "VALUES(%s,%s,0,%s) "
                "ON DUPLICATE KEY UPDATE threshold=VALUES(threshold)",
                (newspaper_id, issue_no, threshold),
            )
            conn.commit()
            affected = cur.rowcount
        OperationLogService.record(
            operator_id, operator_name, "报刊入库", "设置阈值",
            detail={"newspaper_id": newspaper_id, "issue_no": issue_no,
                    "threshold": threshold},
        )
        return affected


# ================================================================
# 四、库存盘点（节点 85）
# ================================================================
class StockCheckService:
    """库存盘点：记录差异并调整库存。"""

    @staticmethod
    def check(
        newspaper_id: int,
        issue_no: str,
        actual_qty: int,
        remark: Optional[str] = None,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        库存盘点（节点 85）。
        读当前系统库存 system_qty，计算 diff=actual_qty-system_qty，
        写盘点记录并把库存调整为 actual_qty（差异调整）。
        返回 {"check_id": 记录ID, "diff": 差异, "system_qty":.., "actual_qty":..}。
        """
        if actual_qty < 0:
            raise ValueError(f"实盘库存不能为负数，当前为 {actual_qty}")

        with get_conn() as conn:
            cur = conn.cursor()
            # 读系统库存（不存在按 0 处理）
            cur.execute(
                "SELECT quantity FROM biz_stock "
                "WHERE newspaper_id=%s AND issue_no=%s",
                (newspaper_id, issue_no),
            )
            row = cur.fetchone()
            system_qty = row["quantity"] if row else 0
            diff = actual_qty - system_qty

            # 写盘点记录
            cur.execute(
                "INSERT INTO biz_stock_check"
                "(newspaper_id, issue_no, system_qty, actual_qty, diff, remark, "
                " operator_id, operator_name) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (newspaper_id, issue_no, system_qty, actual_qty, diff, remark,
                 operator_id, operator_name),
            )
            check_id = cur.lastrowid

            # 调整库存为实盘值
            cur.execute(
                "INSERT INTO biz_stock(newspaper_id, issue_no, quantity) "
                "VALUES(%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE quantity=VALUES(quantity)",
                (newspaper_id, issue_no, actual_qty),
            )
            conn.commit()

        OperationLogService.record(
            operator_id, operator_name, "报刊入库", "库存盘点",
            detail={"newspaper_id": newspaper_id, "issue_no": issue_no,
                    "system_qty": system_qty, "actual_qty": actual_qty, "diff": diff},
        )
        logger.info("盘点 newspaper_id=%s issue_no=%s 系统=%s 实盘=%s 差异=%s",
                    newspaper_id, issue_no, system_qty, actual_qty, diff)
        return {"check_id": check_id, "diff": diff,
                "system_qty": system_qty, "actual_qty": actual_qty}

    @staticmethod
    def list_checks(
        newspaper_id: Optional[int] = None,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """分页查询盘点记录，可按报刊筛选。"""
        where: List[str] = []
        params: List[Any] = []
        if newspaper_id is not None:
            where.append("c.newspaper_id=%s")
            params.append(newspaper_id)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        offset = (page - 1) * size

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT COUNT(*) AS total FROM biz_stock_check c {where_sql}",
                params)
            total = cur.fetchone()["total"]
            cur.execute(
                f"SELECT c.id, c.newspaper_id, n.name AS newspaper_name, "
                f"c.issue_no, c.system_qty, c.actual_qty, c.diff, c.remark, "
                f"c.operator_id, c.operator_name, c.create_time "
                f"FROM biz_stock_check c "
                f"LEFT JOIN biz_newspaper n ON n.id = c.newspaper_id "
                f"{where_sql} ORDER BY c.id DESC LIMIT %s OFFSET %s",
                params + [size, offset],
            )
            rows = cur.fetchall()
        return {"total": total, "page": page, "size": size, "list": rows}


# ================================================================
# 五、对外统一入口（供 Web 层 / 命令行测试调用）
# ================================================================
def main() -> None:
    """命令行自测：建表 + 打印各 Service 提示。"""
    init_tables()
    print("[OK] 报刊入库管理模块已就绪：")
    print("  - StockInService     入库登记 / 批量入库 / 入库流水查询")
    print("  - StockService       库存查询 / 库存预警 / 阈值设置")
    print("  - StockCheckService  库存盘点 / 差异调整 / 盘点记录")


if __name__ == "__main__":
    main()
