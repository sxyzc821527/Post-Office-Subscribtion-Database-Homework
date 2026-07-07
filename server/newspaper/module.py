# -*- coding: utf-8 -*-
"""
================================================================
报刊数据管理模块（业务模块）
================================================================
对应流程图节点：
    · 节点 38  报刊数据管理
    · 节点 60  报刊信息维护（新增/编辑/停刊，支持批量导入）
    · 节点 62  分类管理（树形分类，parent_id）
    · 节点 63  价格策略（单价、订阅周期、折扣）
    · 节点 64  报刊检索（按名称、分类、CN号检索）

技术栈：Python + pymysql + MySQL
设计原则：
    · 前后端分离，本文件为后端核心逻辑，提供可被 Web 层调用的 API 函数。
    · 复用 master/authority.py 中的 get_conn() 与 OperationLogService，
      避免重复造轮子，统一数据库连接入口与审计口径。
    · 分类采用树形结构（parent_id），支持层级查询与树形组装。
    · 批量导入采用容错机制：逐条处理，失败的行记入 fail 列表，成功的行递增。
================================================================
"""

import json
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
logger = logging.getLogger("newspaper")


# ================================================================
# 一、建表 DDL（对应节点 60 / 62 / 63 / 64）
# ================================================================
INIT_SQL_LIST: List[str] = [
    # ---------- 报刊分类表（节点 62） ----------
    """
    CREATE TABLE IF NOT EXISTS `biz_category` (
        `id`          BIGINT       NOT NULL AUTO_INCREMENT COMMENT '分类ID',
        `name`        VARCHAR(64)  NOT NULL COMMENT '分类名',
        `parent_id`   BIGINT       NOT NULL DEFAULT 0 COMMENT '父分类ID，0=顶级',
        `sort`        INT          NOT NULL DEFAULT 0 COMMENT '排序',
        `create_time` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
        PRIMARY KEY (`id`),
        KEY `idx_parent` (`parent_id`),
        KEY `idx_name` (`name`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='报刊分类表';
    """,
    # ---------- 报刊信息表（节点 60 / 63） ----------
    """
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
        `create_time`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
        `update_time`   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                      ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
        PRIMARY KEY (`id`),
        UNIQUE KEY `uk_paper_no` (`paper_no`),
        KEY `idx_category` (`category_id`),
        KEY `idx_name` (`name`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='报刊信息表';
    """,
]


def init_tables() -> None:
    """初始化报刊相关表结构（幂等，已存在则跳过）。"""
    with get_conn() as conn:
        cur = conn.cursor()
        for sql in INIT_SQL_LIST:
            cur.execute(sql)
        conn.commit()
    logger.info("报刊数据管理表结构初始化完成（biz_category / biz_newspaper）。")


# ================================================================
# 二、分类管理（节点 62）
# ================================================================
class CategoryService:
    """报刊分类树形管理：新增、编辑、删除、查询、树形组装。"""

    # ---------- 增 ----------
    @staticmethod
    def add_category(
        name: str,
        parent_id: int = 0,
        sort: int = 0,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> int:
        """
        新增分类，返回新分类 ID。
        parent_id=0 表示顶级分类。
        """
        sql = (
            "INSERT INTO biz_category"
            "(name, parent_id, sort) VALUES(%s,%s,%s)"
        )
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, (name, parent_id, sort))
            conn.commit()
            new_id = cur.lastrowid

        OperationLogService.record(
            operator_id, operator_name, "报刊分类", "新增",
            detail={"category_id": new_id, "name": name, "parent_id": parent_id},
        )
        logger.info("新增分类 id=%s name=%s parent_id=%s", new_id, name, parent_id)
        return new_id

    # ---------- 改 ----------
    @staticmethod
    def update_category(
        cat_id: int,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
        **fields
    ) -> int:
        """
        修改分类，仅更新传入字段。
        支持字段：name, parent_id, sort
        """
        allowed = {"name", "parent_id", "sort"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return 0
        set_clause = ", ".join(f"{k}=%s" for k in updates)
        params = list(updates.values()) + [cat_id]
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"UPDATE biz_category SET {set_clause} WHERE id=%s", params)
            conn.commit()
            affected = cur.rowcount

        OperationLogService.record(
            operator_id, operator_name, "报刊分类", "修改",
            detail={"category_id": cat_id, "fields": list(updates.keys())},
        )
        return affected

    # ---------- 删 ----------
    @staticmethod
    def delete_category(
        cat_id: int,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> int:
        """
        删除分类，若有子分类则抛异常阻止删除。
        返回受影响行数。
        """
        with get_conn() as conn:
            cur = conn.cursor()
            # 检查是否有子分类
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM biz_category WHERE parent_id=%s",
                (cat_id,),
            )
            cnt = cur.fetchone()["cnt"]
            if cnt > 0:
                raise ValueError(
                    f"分类 id={cat_id} 存在 {cnt} 个子分类，禁止删除（请先删除子分类）")

            cur.execute("DELETE FROM biz_category WHERE id=%s", (cat_id,))
            conn.commit()
            affected = cur.rowcount

        if affected:
            OperationLogService.record(
                operator_id, operator_name, "报刊分类", "删除",
                detail={"category_id": cat_id},
            )
            logger.info("删除分类 id=%s", cat_id)
        return affected

    # ---------- 查：平铺列表 ----------
    @staticmethod
    def list_categories() -> List[Dict[str, Any]]:
        """返回全部分类（平铺，不含树形关系）。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, name, parent_id, sort, create_time "
                "FROM biz_category ORDER BY sort ASC, id ASC"
            )
            return cur.fetchall()

    # ---------- 查：树形组装 ----------
    @staticmethod
    def get_tree() -> List[Dict[str, Any]]:
        """
        组装树形分类结构，每个节点含 children 列表。
        只返回顶级节点（parent_id=0），其下递归包含所有子节点。
        """
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, name, parent_id, sort, create_time "
                "FROM biz_category ORDER BY sort ASC, id ASC"
            )
            all_cats = cur.fetchall()

        # 构建 id -> 节点映射 + 添加 children 字段
        cat_map: Dict[int, Dict[str, Any]] = {}
        for cat in all_cats:
            cat["children"] = []
            cat_map[cat["id"]] = cat

        # 建立父子关系
        roots = []
        for cat in all_cats:
            if cat["parent_id"] == 0:
                roots.append(cat)
            else:
                parent = cat_map.get(cat["parent_id"])
                if parent:
                    parent["children"].append(cat)

        return roots


# ================================================================
# 三、报刊信息管理（节点 60 / 63 / 64）
# ================================================================
class NewspaperService:
    """报刊增删改查、起停、价格策略、批量导入、检索。"""

    # ---------- 增 ----------
    @staticmethod
    def add_newspaper(
        paper_no: str,
        name: str,
        cn_code: Optional[str] = None,
        category_id: Optional[int] = None,
        publish_cycle: Optional[str] = None,
        unit_price: float = 0,
        period_price: Optional[float] = None,
        discount: float = 1.0,
        publisher: Optional[str] = None,
        remark: Optional[str] = None,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> int:
        """
        新增报刊，返回新报刊 ID。
        paper_no 必须唯一（业务编号）。
        """
        sql = (
            "INSERT INTO biz_newspaper"
            "(paper_no, name, cn_code, category_id, publish_cycle, "
            " unit_price, period_price, discount, publisher, remark) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        )
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, (
                paper_no, name, cn_code, category_id, publish_cycle,
                unit_price, period_price, discount, publisher, remark,
            ))
            conn.commit()
            new_id = cur.lastrowid

        OperationLogService.record(
            operator_id, operator_name, "报刊信息", "新增",
            detail={"newspaper_id": new_id, "paper_no": paper_no, "name": name},
        )
        logger.info("新增报刊 id=%s paper_no=%s name=%s", new_id, paper_no, name)
        return new_id

    # ---------- 改 ----------
    @staticmethod
    def update_newspaper(
        paper_id: int,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
        **fields
    ) -> int:
        """
        修改报刊，仅更新传入字段。
        支持字段：name, cn_code, category_id, publish_cycle,
                  unit_price, period_price, discount, publisher, remark, status
        """
        allowed = {
            "name", "cn_code", "category_id", "publish_cycle",
            "unit_price", "period_price", "discount", "publisher", "remark", "status",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        # 空串 -> None（避免 '' 无法写入整数列，如 category_id）；
        # 数值列做类型转换。
        int_cols = {"category_id", "status"}
        dec_cols = {"unit_price", "period_price", "discount"}
        cleaned = {}
        for k, v in updates.items():
            if v == "" or v is None:
                cleaned[k] = None
            elif k in int_cols:
                cleaned[k] = int(v)
            elif k in dec_cols:
                cleaned[k] = float(v)
            else:
                cleaned[k] = v
        updates = cleaned
        if not updates:
            return 0
        set_clause = ", ".join(f"{k}=%s" for k in updates)
        params = list(updates.values()) + [paper_id]
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"UPDATE biz_newspaper SET {set_clause} WHERE id=%s", params)
            conn.commit()
            affected = cur.rowcount

        OperationLogService.record(
            operator_id, operator_name, "报刊信息", "修改",
            detail={"newspaper_id": paper_id, "fields": list(updates.keys())},
        )
        return affected

    # ---------- 起停（节点 60） ----------
    @staticmethod
    def set_status(
        paper_id: int,
        status: int,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> int:
        """
        设置报刊状态：1=在售，0=停刊。
        """
        if status not in (0, 1):
            raise ValueError(f"非法状态值: {status}（应为 0 或 1）")

        return NewspaperService.update_newspaper(
            paper_id, operator_id, operator_name, status=status)

    # ---------- 价格策略（节点 63） ----------
    @staticmethod
    def set_price(
        paper_id: int,
        unit_price: Optional[float] = None,
        period_price: Optional[float] = None,
        discount: Optional[float] = None,
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> int:
        """
        设置报刊价格策略：单期价格、周期价、折扣。
        """
        updates = {}
        if unit_price is not None:
            updates["unit_price"] = unit_price
        if period_price is not None:
            updates["period_price"] = period_price
        if discount is not None:
            updates["discount"] = discount

        if not updates:
            return 0

        return NewspaperService.update_newspaper(
            paper_id, operator_id, operator_name, **updates)

    # ---------- 查：单条 ----------
    @staticmethod
    def get_newspaper(paper_id: int) -> Optional[Dict[str, Any]]:
        """查询单个报刊详情。"""
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, paper_no, name, cn_code, category_id, publish_cycle, "
                "unit_price, period_price, discount, publisher, status, remark, "
                "create_time, update_time FROM biz_newspaper WHERE id=%s",
                (paper_id,),
            )
            return cur.fetchone()

    # ---------- 查：列表（节点 64 检索） ----------
    @staticmethod
    def list_newspapers(
        keyword: str = "",
        category_id: Optional[int] = None,
        cn_code: Optional[str] = None,
        status: Optional[int] = None,
        page: int = 1,
        size: int = 20,
    ) -> Dict[str, Any]:
        """
        分页查询报刊列表，支持多维检索。
        keyword：模糊匹配 name / paper_no。
        category_id：分类ID。
        cn_code：CN号精确匹配。
        status：1=在售，0=停刊，None=全部。
        """
        where: List[str] = []
        params: List[Any] = []

        if keyword:
            where.append("(name LIKE %s OR paper_no LIKE %s)")
            kw = f"%{keyword}%"
            params.extend([kw, kw])
        if category_id is not None:
            where.append("category_id=%s")
            params.append(category_id)
        if cn_code:
            where.append("cn_code=%s")
            params.append(cn_code)
        if status is not None:
            where.append("status=%s")
            params.append(status)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        offset = (page - 1) * size

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT COUNT(*) AS total FROM biz_newspaper {where_sql}", params)
            total = cur.fetchone()["total"]
            cur.execute(
                f"SELECT id, paper_no, name, cn_code, category_id, publish_cycle, "
                f"unit_price, period_price, discount, publisher, status, "
                f"create_time FROM biz_newspaper {where_sql} "
                f"ORDER BY id DESC LIMIT %s OFFSET %s",
                params + [size, offset],
            )
            rows = cur.fetchall()
        return {"total": total, "page": page, "size": size, "list": rows}

    # ---------- 批量导入（节点 60） ----------
    @staticmethod
    def batch_import(
        rows: List[Dict[str, Any]],
        operator_id: Optional[int] = None,
        operator_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        批量导入报刊，容错机制。
        rows：每项为字典，至少包含 paper_no 和 name，其它字段可选。
        返回：{"success": 成功数, "fail": [{"row_index": 行号, "paper_no": 值, "error": 错误信息}]}
        """
        success_count = 0
        fail_list = []

        for idx, row in enumerate(rows):
            try:
                # 验证必填字段
                paper_no = str(row.get("paper_no", "")).strip()
                name = str(row.get("name", "")).strip()
                if not paper_no or not name:
                    raise ValueError("paper_no 和 name 为必填项")

                # 检查 paper_no 是否重复
                with get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT COUNT(*) AS cnt FROM biz_newspaper WHERE paper_no=%s",
                        (paper_no,),
                    )
                    if cur.fetchone()["cnt"] > 0:
                        raise ValueError(f"报刊编号 {paper_no} 已存在")

                # 提取可选字段
                cn_code = row.get("cn_code")
                category_id = row.get("category_id")
                publish_cycle = row.get("publish_cycle")
                unit_price = row.get("unit_price", 0)
                period_price = row.get("period_price")
                discount = row.get("discount", 1.0)
                publisher = row.get("publisher")
                remark = row.get("remark")

                # 新增报刊
                NewspaperService.add_newspaper(
                    paper_no=paper_no,
                    name=name,
                    cn_code=cn_code,
                    category_id=category_id,
                    publish_cycle=publish_cycle,
                    unit_price=unit_price,
                    period_price=period_price,
                    discount=discount,
                    publisher=publisher,
                    remark=remark,
                    operator_id=operator_id,
                    operator_name=operator_name,
                )
                success_count += 1

            except Exception as e:
                fail_list.append({
                    "row_index": idx,
                    "paper_no": row.get("paper_no", ""),
                    "error": str(e),
                })
                logger.warning("批量导入第 %d 行失败: %s", idx, str(e))

        OperationLogService.record(
            operator_id, operator_name, "报刊信息", "批量导入",
            detail={"success": success_count, "fail_count": len(fail_list)},
        )
        logger.info("批量导入完成：成功 %d 条，失败 %d 条", success_count, len(fail_list))
        return {"success": success_count, "fail": fail_list}


# ================================================================
# 四、对外统一入口（供 Web 层 / 命令行测试调用）
# ================================================================
def main() -> None:
    """命令行自测：建表 + 打印各 Service 提示。"""
    init_tables()
    print("[OK] 报刊数据管理模块已就绪：")
    print("  - CategoryService        报刊分类树形管理（CRUD / 树形组装）")
    print("  - NewspaperService       报刊信息管理（CRUD / 起停 / 价格策略 / 检索 / 批量导入）")


if __name__ == "__main__":
    main()
