# -*- coding: utf-8 -*-
"""
================================================================
客户数据管理 —— Web / API 层（原生 http.server，零第三方依赖）
================================================================
在 staffsystem/customer.py 的 Service 层之上，提供 HTTP RESTful 接口，
供前端（fetch / axios）或 Postman 调用。

对应流程图：节点 40 客户数据管理（68 档案 / 69 地址 / 70 标签 / 71 订阅历史）。

权限设计（接入 master/authority.py）：
    · 客户管理属于 O3（客户/订阅管理员），权限点 api:customer:*。
    · O5 超级管理员拥有 "*" 通配符，自动放行。
    · 调用方需在请求头携带 X-Emp-Id 标识操作者，后端据此鉴权 + 记日志。
    · 鉴权失败统一返回 403。

启动：
    python staffsystem/api_server.py            # 默认 0.0.0.0:8088
    python staffsystem/api_server.py 9000       # 自定义端口

接口约定：
    统一响应体：
        { "code": 0, "msg": "ok", "data": <任意> }
        code=0 成功；code!=0 失败（403 鉴权失败 / 400 参数错误 / 404 未找到 / 500 内部错误）
================================================================
"""

import json
import logging
import os
import re
import sys
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

# 业务模块以子包方式导入（绝对导入，无需 sys.path 操作）
from server import customer as cust  # noqa: E402
from server.customer import (  # noqa: E402
    CustomerAddressService,
    CustomerService,
    CustomerTagService,
    SubscriptionHistoryService,
    init_tables,
)
# 各业务模块以命名空间方式导入，避免 init_tables 等同名函数冲突
from server import newspaper as newspaper_mod  # noqa: E402
from server import subscription as subscription_mod  # noqa: E402
from server import stock as stock_mod  # noqa: E402
from server import delivery as delivery_mod  # noqa: E402
from server.core.authority import (  # noqa: E402
    EmployeeService,
    OperationLogService,
    PermissionChecker,
    PermissionService,
    ProfileService,
    init_database,
    verify_password,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("api")


# ================================================================
# 一、路由表定义
# ================================================================
# 每条路由：(method, regex, handler, perm_code)
#   perm_code 形如 "customer:list"，会被 api:customer:* 通配符命中
# handler 签名: handler(handler_obj, path_params: Dict[str,str],
#                       query: Dict[str,str], body: Dict) -> (code:int, msg, data)
INT = r"(\d+)"                 # 整数路径参数
NAME = r"([^/]+)"              # 任意非斜杠片段（标签名等）

# ROUTES 列表里的处理函数定义在下方第三节，故此处用惰性函数延迟构造，
# 避免 Python 加载时立即求值导致 NameError（函数尚未定义）。
_ROUTES_CACHE: Optional[List[Tuple[str, str, Callable, str]]] = None


def _get_routes() -> List[Tuple[str, str, Callable, str]]:
    global _ROUTES_CACHE
    if _ROUTES_CACHE is not None:
        return _ROUTES_CACHE
    _ROUTES_CACHE = [
        # ---------- 客户档案（节点 68） ----------
        ("GET",    r"^/api/customers$",                                  list_customers,    "customer:list"),
        ("GET",    rf"^/api/customers/{INT}$",                            get_customer,      "customer:read"),
        ("POST",   r"^/api/customers$",                                  create_customer,   "customer:create"),
        ("PUT",    rf"^/api/customers/{INT}$",                            update_customer,   "customer:update"),
        ("DELETE", rf"^/api/customers/{INT}$",                            delete_customer,   "customer:delete"),
        ("PUT",    rf"^/api/customers/{INT}/status$",                     set_status,        "customer:update"),
        ("PUT",    rf"^/api/customers/{INT}/password$",                   reset_password,    "customer:update"),

    # ---------- 地址管理（节点 69） ----------
    ("GET",    rf"^/api/customers/{INT}/addresses$",                  list_addresses,    "customer:read"),
    ("POST",   rf"^/api/customers/{INT}/addresses$",                  add_address,       "customer:update"),
    ("PUT",    rf"^/api/addresses/{INT}$",                            update_address,    "customer:update"),
    ("DELETE", rf"^/api/addresses/{INT}$",                            delete_address,    "customer:update"),
    ("PUT",    rf"^/api/customers/{INT}/addresses/{INT}/default$",    set_default_addr,  "customer:update"),

    # ---------- 客户标签（节点 70） ----------
    ("GET",    rf"^/api/customers/{INT}/tags$",                       list_tags,         "customer:read"),
    ("POST",   rf"^/api/customers/{INT}/tags$",                       set_tag,           "customer:update"),
    ("DELETE", rf"^/api/customers/{INT}/tags/{NAME}$",                remove_tag,        "customer:update"),
    ("GET",    rf"^/api/tags/{NAME}/customers$",                      find_by_tag,       "customer:list"),

    # ---------- 订阅历史（节点 71） ----------
        ("GET",    rf"^/api/customers/{INT}/subscriptions$",              sub_history,       "customer:read"),
        ("GET",    rf"^/api/customers/{INT}/subscriptions/summary$",      sub_summary,       "customer:read"),
    ]
    return _ROUTES_CACHE


# ================================================================
# 二、辅助工具
# ================================================================
def _operator_name(emp_id: Optional[int]) -> Optional[str]:
    """根据 emp_id 取员工真实姓名，用于操作日志。"""
    if not emp_id:
        return None
    try:
        emp = EmployeeService.get_employee(emp_id)
        return (emp or {}).get("real_name") or (emp or {}).get("username")
    except Exception:  # 鉴权信息缺失时不影响主流程
        return None


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _opt_int(v: Any) -> Optional[int]:
    if v in (None, "", "null"):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ================================================================
# 三、路由处理函数
# ================================================================
# 约定：返回 (code, msg, data)；code=0 成功
# path_params 是正则捕获组映射成具名 dict，便于阅读

# ---------- 客户档案 ----------
def list_customers(h, p, q, b):
    res = CustomerService.list_customers(
        keyword=q.get("keyword", ""),
        cust_type=q.get("type") or None,
        status=_opt_int(q.get("status")),
        level=q.get("level") or None,
        page=max(_int(q.get("page"), 1), 1),
        size=max(_int(q.get("size"), 20), 1),
    )
    return 0, "ok", res


def get_customer(h, p, q, b):
    cid = int(p["id"])
    row = CustomerService.get_customer(cid)
    if not row:
        return 404, "客户不存在", None
    return 0, "ok", row


def create_customer(h, p, q, b):
    required = ("cust_no", "cust_type", "name")
    miss = [k for k in required if not b.get(k)]
    if miss:
        return 400, f"缺少必填字段: {miss}", None
    emp_id = h.operator_id
    new_id = CustomerService.add_customer(
        cust_no=b["cust_no"], cust_type=b["cust_type"], name=b["name"],
        id_card=b.get("id_card"), org_code=b.get("org_code"),
        contact_person=b.get("contact_person"), phone=b.get("phone"),
        email=b.get("email"), password=b.get("password"),
        level=b.get("level"), remark=b.get("remark"),
        operator_id=emp_id, operator_name=_operator_name(emp_id),
    )
    return 0, "新增成功", {"id": new_id}


def update_customer(h, p, q, b):
    cid = int(p["id"])
    emp_id = h.operator_id
    fields = {k: v for k, v in b.items()
              if k in {"cust_type", "name", "id_card", "org_code",
                       "contact_person", "phone", "email",
                       "level", "status", "remark"}}
    affected = CustomerService.update_customer(
        cid, operator_id=emp_id,
        operator_name=_operator_name(emp_id), **fields)
    return 0, "修改成功", {"affected": affected}


def delete_customer(h, p, q, b):
    cid = int(p["id"])
    emp_id = h.operator_id
    affected = CustomerService.delete_customer(
        cid, operator_id=emp_id, operator_name=_operator_name(emp_id))
    if not affected:
        return 404, "客户不存在", None
    return 0, "删除成功", {"affected": affected}


def set_status(h, p, q, b):
    cid = int(p["id"])
    status = _int(b.get("status"), -1)
    if status not in (0, 1):
        return 400, "status 必须为 0(停用) 或 1(启用)", None
    emp_id = h.operator_id
    affected = CustomerService.set_status(
        cid, status, operator_id=emp_id,
        operator_name=_operator_name(emp_id))
    return 0, "状态已更新", {"affected": affected}


def reset_password(h, p, q, b):
    cid = int(p["id"])
    pwd = b.get("password")
    if not pwd:
        return 400, "缺少 password", None
    emp_id = h.operator_id
    affected = CustomerService.reset_password(
        cid, pwd, operator_id=emp_id,
        operator_name=_operator_name(emp_id))
    return 0, "密码已重置", {"affected": affected}


# ---------- 地址管理 ----------
def list_addresses(h, p, q, b):
    cid = int(p["id"])
    return 0, "ok", CustomerAddressService.list_addresses(cid)


def add_address(h, p, q, b):
    cid = int(p["id"])
    if not b.get("recipient") or not b.get("detail"):
        return 400, "缺少 recipient / detail", None
    emp_id = h.operator_id
    new_id = CustomerAddressService.add_address(
        cid, recipient=b["recipient"], detail=b["detail"],
        phone=b.get("phone"), province=b.get("province"),
        city=b.get("city"), district=b.get("district"),
        is_default=bool(b.get("is_default")),
        operator_id=emp_id, operator_name=_operator_name(emp_id))
    return 0, "新增成功", {"id": new_id}


def update_address(h, p, q, b):
    aid = int(p["id"])
    emp_id = h.operator_id
    fields = {k: v for k, v in b.items()
              if k in {"recipient", "phone", "province",
                       "city", "district", "detail"}}
    affected = CustomerAddressService.update_address(
        aid, operator_id=emp_id,
        operator_name=_operator_name(emp_id), **fields)
    return 0, "修改成功", {"affected": affected}


def delete_address(h, p, q, b):
    aid = int(p["id"])
    emp_id = h.operator_id
    affected = CustomerAddressService.delete_address(
        aid, operator_id=emp_id, operator_name=_operator_name(emp_id))
    if not affected:
        return 404, "地址不存在", None
    return 0, "删除成功", {"affected": affected}


def set_default_addr(h, p, q, b):
    cid = int(p["id"])         # 路径第一个 INT：customer_id
    aid = int(p["aid"])        # 第二个 INT：address_id
    emp_id = h.operator_id
    affected = CustomerAddressService.set_default(
        aid, cid, operator_id=emp_id,
        operator_name=_operator_name(emp_id))
    if not affected:
        return 404, "地址不存在或不属于该客户", None
    return 0, "已设为默认", {"affected": affected}


# ---------- 客户标签 ----------
def list_tags(h, p, q, b):
    cid = int(p["id"])
    return 0, "ok", CustomerTagService.list_tags(cid)


def set_tag(h, p, q, b):
    cid = int(p["id"])
    tag_name = b.get("tag_name")
    if not tag_name:
        return 400, "缺少 tag_name", None
    emp_id = h.operator_id
    affected = CustomerTagService.set_tag(
        cid, tag_name, b.get("tag_value"),
        operator_id=emp_id, operator_name=_operator_name(emp_id))
    return 0, "标签已设置", {"affected": affected}


def remove_tag(h, p, q, b):
    cid = int(p["id"])
    tag_name = p["name"]
    emp_id = h.operator_id
    affected = CustomerTagService.remove_tag(
        cid, tag_name, operator_id=emp_id,
        operator_name=_operator_name(emp_id))
    if not affected:
        return 404, "该客户无此标签", None
    return 0, "标签已移除", {"affected": affected}


def find_by_tag(h, p, q, b):
    tag_name = p["name"]
    rows = CustomerTagService.find_customers_by_tag(
        tag_name, tag_value=q.get("value") or None)
    return 0, "ok", rows


# ---------- 订阅历史 ----------
def sub_history(h, p, q, b):
    cid = int(p["id"])
    res = SubscriptionHistoryService.list_by_customer(
        cid, page=max(_int(q.get("page"), 1), 1),
        size=max(_int(q.get("size"), 20), 1))
    return 0, "ok", res


def sub_summary(h, p, q, b):
    cid = int(p["id"])
    return 0, "ok", SubscriptionHistoryService.summary(cid)


# ================================================================
# 四、HTTP 请求处理器
# ================================================================
class _RouteMatch:
    __slots__ = ("handler", "perm", "params")


def _match_route(method: str, path: str) -> Optional[_RouteMatch]:
    """按路由表匹配，返回处理函数 / 权限点 / 命名参数。"""
    for m, pattern, handler, perm in _get_routes():
        if m != method:
            continue
        matched = re.match(pattern, path)
        if not matched:
            continue
        # 把捕获组映射成具名参数（按顺序命名）
        groups = matched.groups()
        names: List[str] = []
        for i, g in enumerate(groups):
            names.append(f"g{i}")
        # 对常见模式赋予语义化名字
        # 依据该路由有几个捕获组 + 语义做映射（在 handler 内部也能从 p 里取）
        params: Dict[str, Any] = {f"g{i}": g for i, g in enumerate(groups)}
        # 语义命名（便于 handler 代码可读）
        # 客户 id
        if re.search(r"/customers/(\d+)", path):
            params["id"] = re.search(r"/customers/(\d+)", path).group(1)
        # 地址 id（独立 /api/addresses/<id>）
        am = re.search(r"/addresses/(\d+)", path)
        if am and "/customers/" not in path:
            params["id"] = am.group(1)
        # 子地址 default 的 address_id
        if "/default" in path:
            dm = re.search(r"/customers/(\d+)/addresses/(\d+)/default", path)
            if dm:
                params["id"] = dm.group(1)
                params["aid"] = dm.group(2)
        # 标签名
        tm = re.search(r"/tags/([^/]+)", path)
        if tm:
            params["name"] = tm.group(1)

        rm = _RouteMatch()
        rm.handler = handler
        rm.perm = perm
        rm.params = params
        return rm
    return None


# ================================================================
# 三点五、扩展模块路由（报刊 / 订阅 / 入库 / 发放 / 系统用户 / 个人中心）
# ================================================================
# 扩展路由采用「显式命名捕获组」机制：每条路由额外声明 param_names，
# _match_ext 按顺序把正则捕获组 zip 成具名参数，避免旧匹配器的语义猜测。
def _do_login(body: Dict[str, Any]) -> Tuple[int, str, Any]:
    """
    登录（公开接口，无需 X-Emp-Id）。
    校验用户名 + 密码，成功返回员工基础信息 + 权限等级 + 最终权限集合，
    前端据此保存 emp_id（后续作为 X-Emp-Id）并按权限渲染菜单。
    """
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        return 400, "缺少 username / password", None
    emp = EmployeeService.get_employee_by_username(username)
    if not emp or not verify_password(password, emp["password"]):
        return 401, "用户名或密码错误", None
    if emp.get("status") == 0:
        return 403, "该账号已被停用", None
    emp_id = emp["id"]
    OperationLogService.record(
        emp_id, emp.get("real_name") or emp.get("username"),
        "登录", "登录成功", detail={"username": username})
    return 0, "登录成功", {
        "emp_id": emp_id,
        "username": emp["username"],
        "real_name": emp.get("real_name"),
        "levels": PermissionService.get_employee_levels(emp_id),
        "permissions": PermissionService.get_employee_permissions(emp_id),
    }


def _date(v: Any) -> Optional[date]:
    """把 'YYYY-MM-DD' 字符串解析为 date；空值返回 None，非法格式抛 ValueError。"""
    if v in (None, "", "null"):
        return None
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"日期格式应为 YYYY-MM-DD: {v}")


# ---------- 报刊数据管理（节点 38 / 60 / 62 / 63 / 64） ----------
def ext_list_newspapers(h, p, q, b):
    return 0, "ok", newspaper_mod.NewspaperService.list_newspapers(
        keyword=q.get("keyword", ""),
        category_id=_opt_int(q.get("category_id")),
        cn_code=q.get("cn_code") or None,
        status=_opt_int(q.get("status")),
        page=max(_int(q.get("page"), 1), 1),
        size=max(_int(q.get("size"), 20), 1))


def ext_get_newspaper(h, p, q, b):
    row = newspaper_mod.NewspaperService.get_newspaper(int(p["id"]))
    if not row:
        return 404, "报刊不存在", None
    return 0, "ok", row


def ext_create_newspaper(h, p, q, b):
    if not b.get("paper_no") or not b.get("name"):
        return 400, "缺少 paper_no / name", None
    eid = h.operator_id
    nid = newspaper_mod.NewspaperService.add_newspaper(
        paper_no=b["paper_no"], name=b["name"], cn_code=b.get("cn_code"),
        category_id=_opt_int(b.get("category_id")),
        publish_cycle=b.get("publish_cycle"), unit_price=b.get("unit_price", 0),
        period_price=b.get("period_price"), discount=b.get("discount", 1.0),
        publisher=b.get("publisher"), remark=b.get("remark"),
        operator_id=eid, operator_name=_operator_name(eid))
    return 0, "新增成功", {"id": nid}


def ext_update_newspaper(h, p, q, b):
    eid = h.operator_id
    fields = {k: v for k, v in b.items() if k in {
        "name", "cn_code", "category_id", "publish_cycle", "unit_price",
        "period_price", "discount", "publisher", "remark", "status"}}
    aff = newspaper_mod.NewspaperService.update_newspaper(
        int(p["id"]), operator_id=eid,
        operator_name=_operator_name(eid), **fields)
    return 0, "修改成功", {"affected": aff}


def ext_newspaper_status(h, p, q, b):
    eid = h.operator_id
    aff = newspaper_mod.NewspaperService.set_status(
        int(p["id"]), _int(b.get("status"), -1),
        operator_id=eid, operator_name=_operator_name(eid))
    return 0, "状态已更新", {"affected": aff}


def ext_newspaper_price(h, p, q, b):
    eid = h.operator_id
    aff = newspaper_mod.NewspaperService.set_price(
        int(p["id"]), unit_price=b.get("unit_price"),
        period_price=b.get("period_price"), discount=b.get("discount"),
        operator_id=eid, operator_name=_operator_name(eid))
    return 0, "价格已更新", {"affected": aff}


def ext_newspaper_batch(h, p, q, b):
    rows = b.get("rows") or []
    if not isinstance(rows, list):
        return 400, "rows 必须为数组", None
    eid = h.operator_id
    return 0, "批量导入完成", newspaper_mod.NewspaperService.batch_import(
        rows, operator_id=eid, operator_name=_operator_name(eid))


def ext_list_categories(h, p, q, b):
    return 0, "ok", newspaper_mod.CategoryService.list_categories()


def ext_category_tree(h, p, q, b):
    return 0, "ok", newspaper_mod.CategoryService.get_tree()


def ext_add_category(h, p, q, b):
    if not b.get("name"):
        return 400, "缺少 name", None
    eid = h.operator_id
    cid = newspaper_mod.CategoryService.add_category(
        b["name"], parent_id=_int(b.get("parent_id"), 0),
        sort=_int(b.get("sort"), 0),
        operator_id=eid, operator_name=_operator_name(eid))
    return 0, "新增成功", {"id": cid}


def ext_update_category(h, p, q, b):
    eid = h.operator_id
    fields = {k: v for k, v in b.items() if k in {"name", "parent_id", "sort"}}
    aff = newspaper_mod.CategoryService.update_category(
        int(p["id"]), operator_id=eid,
        operator_name=_operator_name(eid), **fields)
    return 0, "修改成功", {"affected": aff}


def ext_delete_category(h, p, q, b):
    eid = h.operator_id
    aff = newspaper_mod.CategoryService.delete_category(
        int(p["id"]), operator_id=eid, operator_name=_operator_name(eid))
    return 0, "删除成功", {"affected": aff}


# ---------- 订阅管理（节点 42 / 76 / 77 / 78 / 79） ----------
def ext_list_subs(h, p, q, b):
    return 0, "ok", subscription_mod.SubscriptionService.list_subscriptions(
        customer_id=_opt_int(q.get("customer_id")),
        newspaper_id=_opt_int(q.get("newspaper_id")),
        status=q.get("status") or None,
        page=max(_int(q.get("page"), 1), 1),
        size=max(_int(q.get("size"), 20), 1))


def ext_get_sub(h, p, q, b):
    row = subscription_mod.SubscriptionService.get_subscription(int(p["id"]))
    if not row:
        return 404, "订阅不存在", None
    return 0, "ok", row


def ext_create_sub(h, p, q, b):
    for k in ("customer_id", "newspaper_id", "start_date", "end_date", "periods"):
        if b.get(k) in (None, ""):
            return 400, f"缺少 {k}", None
    eid = h.operator_id
    nid = subscription_mod.SubscriptionService.create_subscription(
        customer_id=int(b["customer_id"]), newspaper_id=int(b["newspaper_id"]),
        start_date=_date(b["start_date"]), end_date=_date(b["end_date"]),
        periods=int(b["periods"]), address_id=_opt_int(b.get("address_id")),
        remark=b.get("remark"), operator_id=eid, operator_name=_operator_name(eid))
    return 0, "新建成功", {"id": nid}


def ext_cancel_sub(h, p, q, b):
    eid = h.operator_id
    return 0, "退订成功", subscription_mod.SubscriptionService.cancel_subscription(
        int(p["id"]), operator_id=eid, operator_name=_operator_name(eid))


def ext_change_sub(h, p, q, b):
    if not b.get("new_newspaper_id"):
        return 400, "缺少 new_newspaper_id", None
    eid = h.operator_id
    return 0, "换订成功", subscription_mod.SubscriptionService.change_subscription(
        int(p["id"]), int(b["new_newspaper_id"]),
        operator_id=eid, operator_name=_operator_name(eid))


def ext_sub_expiring(h, p, q, b):
    return 0, "ok", subscription_mod.RenewalService.list_expiring(
        days=_int(q.get("days"), 7))


def ext_sub_stat_newspaper(h, p, q, b):
    return 0, "ok", subscription_mod.SubscriptionStatService.by_newspaper()


def ext_sub_stat_custtype(h, p, q, b):
    return 0, "ok", subscription_mod.SubscriptionStatService.by_customer_type()


def ext_sub_stat_period(h, p, q, b):
    s, e = _date(q.get("start")), _date(q.get("end"))
    if not s or not e:
        return 400, "缺少 start / end (YYYY-MM-DD)", None
    return 0, "ok", subscription_mod.SubscriptionStatService.by_period(s, e)


# ---------- 报刊入库管理（节点 44 / 84 / 85 / 86 / 87） ----------
def ext_stock_in(h, p, q, b):
    for k in ("newspaper_id", "issue_no", "quantity"):
        if b.get(k) in (None, ""):
            return 400, f"缺少 {k}", None
    eid = h.operator_id
    return 0, "入库成功", stock_mod.StockInService.stock_in(
        int(b["newspaper_id"]), str(b["issue_no"]), int(b["quantity"]),
        threshold=_opt_int(b.get("threshold")),
        operator_id=eid, operator_name=_operator_name(eid))


def ext_stock_in_batch(h, p, q, b):
    rows = b.get("rows") or []
    if not isinstance(rows, list):
        return 400, "rows 必须为数组", None
    eid = h.operator_id
    return 0, "批量入库完成", stock_mod.StockInService.batch_stock_in(
        rows, operator_id=eid, operator_name=_operator_name(eid))


def ext_list_stock_in(h, p, q, b):
    return 0, "ok", stock_mod.StockInService.list_stock_in(
        newspaper_id=_opt_int(q.get("newspaper_id")),
        start=q.get("start") or None, end=q.get("end") or None,
        page=max(_int(q.get("page"), 1), 1),
        size=max(_int(q.get("size"), 20), 1))


def ext_list_stock(h, p, q, b):
    return 0, "ok", stock_mod.StockService.list_stock(
        newspaper_id=_opt_int(q.get("newspaper_id")),
        only_warning=q.get("only_warning") in ("1", "true", "True"),
        page=max(_int(q.get("page"), 1), 1),
        size=max(_int(q.get("size"), 20), 1))


def ext_stock_warning(h, p, q, b):
    return 0, "ok", stock_mod.StockService.warning_list()


def ext_stock_threshold(h, p, q, b):
    for k in ("newspaper_id", "issue_no", "threshold"):
        if b.get(k) in (None, ""):
            return 400, f"缺少 {k}", None
    eid = h.operator_id
    aff = stock_mod.StockService.set_threshold(
        int(b["newspaper_id"]), str(b["issue_no"]), int(b["threshold"]),
        operator_id=eid, operator_name=_operator_name(eid))
    return 0, "阈值已设置", {"affected": aff}


def ext_stock_check(h, p, q, b):
    for k in ("newspaper_id", "issue_no", "actual_qty"):
        if b.get(k) in (None, ""):
            return 400, f"缺少 {k}", None
    eid = h.operator_id
    return 0, "盘点完成", stock_mod.StockCheckService.check(
        int(b["newspaper_id"]), str(b["issue_no"]), int(b["actual_qty"]),
        remark=b.get("remark"),
        operator_id=eid, operator_name=_operator_name(eid))


def ext_list_checks(h, p, q, b):
    return 0, "ok", stock_mod.StockCheckService.list_checks(
        newspaper_id=_opt_int(q.get("newspaper_id")),
        page=max(_int(q.get("page"), 1), 1),
        size=max(_int(q.get("size"), 20), 1))


# ---------- 报刊发放系统（节点 46 / 92 / 93 / 94 / 95） ----------
def ext_gen_tasks(h, p, q, b):
    d = _date(b.get("deliver_date"))
    if not d:
        return 400, "缺少 deliver_date (YYYY-MM-DD)", None
    eid = h.operator_id
    return 0, "生成完成", delivery_mod.DeliveryTaskService.generate_daily_tasks(
        d, operator_id=eid, operator_name=_operator_name(eid))


def ext_assign_tasks(h, p, q, b):
    d = _date(b.get("deliver_date"))
    mapping = b.get("mapping") or {}
    if not d:
        return 400, "缺少 deliver_date", None
    if not isinstance(mapping, dict) or not mapping:
        return 400, "缺少 mapping{区域:员工id}", None
    mapping = {str(k): int(v) for k, v in mapping.items()}
    eid = h.operator_id
    return 0, "分配完成", delivery_mod.DeliveryTaskService.assign_by_district(
        d, mapping, operator_id=eid, operator_name=_operator_name(eid))


def ext_list_tasks(h, p, q, b):
    return 0, "ok", delivery_mod.DeliveryTaskService.list_tasks(
        deliver_date=_date(q.get("deliver_date")),
        courier_id=_opt_int(q.get("courier_id")),
        status=q.get("status") or None,
        customer_id=_opt_int(q.get("customer_id")),
        page=max(_int(q.get("page"), 1), 1),
        size=max(_int(q.get("size"), 20), 1))


def ext_sign_task(h, p, q, b):
    eid = h.operator_id
    aff = delivery_mod.DeliveryTaskService.sign(
        int(p["id"]), operator_id=eid, operator_name=_operator_name(eid))
    return 0, "已签收", {"affected": aff}


def ext_abnormal_task(h, p, q, b):
    if not b.get("remark"):
        return 400, "缺少 remark", None
    eid = h.operator_id
    aff = delivery_mod.DeliveryTaskService.report_abnormal(
        int(p["id"]), str(b["remark"]),
        operator_id=eid, operator_name=_operator_name(eid))
    return 0, "已上报", {"affected": aff}


def ext_report_missing(h, p, q, b):
    if b.get("task_id") in (None, ""):
        return 400, "缺少 task_id", None
    eid = h.operator_id
    mid = delivery_mod.MissingService.report_missing(
        int(b["task_id"]), str(b.get("reason") or ""),
        operator_id=eid, operator_name=_operator_name(eid))
    return 0, "缺刊已登记", {"missing_id": mid}


def ext_reissue(h, p, q, b):
    d = _date(b.get("deliver_date"))
    if not d:
        return 400, "缺少 deliver_date", None
    eid = h.operator_id
    return 0, "补发已生成", delivery_mod.MissingService.reissue(
        int(p["id"]), d, operator_id=eid, operator_name=_operator_name(eid))


def ext_list_missing(h, p, q, b):
    return 0, "ok", delivery_mod.MissingService.list_missing(
        status=q.get("status") or None,
        page=max(_int(q.get("page"), 1), 1),
        size=max(_int(q.get("size"), 20), 1))


# ---------- 系统用户管理（节点 48 / 100 / 101 / 103，需 O5 或对应权限） ----------
def ext_list_employees(h, p, q, b):
    return 0, "ok", EmployeeService.list_employees(
        keyword=q.get("keyword", ""), status=_opt_int(q.get("status")),
        page=max(_int(q.get("page"), 1), 1), size=max(_int(q.get("size"), 20), 1))


def ext_get_employee(h, p, q, b):
    row = EmployeeService.get_employee(int(p["id"]))
    if not row:
        return 404, "员工不存在", None
    return 0, "ok", row


def ext_add_employee(h, p, q, b):
    for k in ("emp_no", "username", "password"):
        if not b.get(k):
            return 400, f"缺少 {k}", None
    nid = EmployeeService.add_employee(
        b["emp_no"], b["username"], b["password"],
        real_name=b.get("real_name"), phone=b.get("phone"))
    return 0, "新增成功", {"id": nid}


def ext_update_employee(h, p, q, b):
    fields = {k: v for k, v in b.items() if k in {"real_name", "phone", "status"}}
    aff = EmployeeService.update_employee(int(p["id"]), **fields)
    return 0, "修改成功", {"affected": aff}


def ext_delete_employee(h, p, q, b):
    aff = EmployeeService.delete_employee(int(p["id"]))
    if not aff:
        return 404, "员工不存在", None
    return 0, "删除成功", {"affected": aff}


def ext_employee_status(h, p, q, b):
    st = _int(b.get("status"), -1)
    if st not in (0, 1):
        return 400, "status 必须为 0/1", None
    aff = EmployeeService.set_status(int(p["id"]), st)
    return 0, "状态已更新", {"affected": aff}


def ext_employee_password(h, p, q, b):
    if not b.get("password"):
        return 400, "缺少 password", None
    aff = EmployeeService.reset_password(int(p["id"]), b["password"])
    return 0, "密码已重置", {"affected": aff}


def ext_employee_levels(h, p, q, b):
    return 0, "ok", PermissionService.get_employee_levels(int(p["id"]))


def ext_assign_level(h, p, q, b):
    if not b.get("level"):
        return 400, "缺少 level", None
    aff = PermissionService.assign_level(int(p["id"]), b["level"])
    return 0, "已分配", {"affected": aff}


def ext_revoke_level(h, p, q, b):
    aff = PermissionService.revoke_level(int(p["id"]), p["level"])
    return 0, "已撤销", {"affected": aff}


def ext_employee_perms(h, p, q, b):
    return 0, "ok", PermissionService.get_employee_permissions(int(p["id"]))


def ext_grant_perm(h, p, q, b):
    if not b.get("perm_code"):
        return 400, "缺少 perm_code", None
    aff = PermissionService.grant_permission(int(p["id"]), b["perm_code"])
    return 0, "已授予", {"affected": aff}


def ext_revoke_perm(h, p, q, b):
    aff = PermissionService.revoke_permission(int(p["id"]), p["code"])
    return 0, "已撤销", {"affected": aff}


def ext_list_levels(h, p, q, b):
    return 0, "ok", PermissionService.list_levels()


def ext_create_level(h, p, q, b):
    if not b.get("level") or not b.get("name"):
        return 400, "缺少 level / name", None
    PermissionService.create_level(b["level"], b["name"], b.get("desc", ""))
    return 0, "已创建", {"level": b["level"]}


def ext_list_logs(h, p, q, b):
    return 0, "ok", OperationLogService.list_logs(
        emp_id=_opt_int(q.get("emp_id")), module=q.get("module") or None,
        start=q.get("start") or None, end=q.get("end") or None,
        page=max(_int(q.get("page"), 1), 1), size=max(_int(q.get("size"), 50), 1))


# ---------- 个人中心（节点 102，任意登录员工，仅作用于本人） ----------
def ext_profile(h, p, q, b):
    prof = ProfileService.get_profile(h.operator_id)
    if not prof:
        return 404, "账号不存在", None
    return 0, "ok", prof


def ext_change_pwd(h, p, q, b):
    if not b.get("old_password") or not b.get("new_password"):
        return 400, "缺少 old_password / new_password", None
    aff = ProfileService.change_own_password(
        h.operator_id, b["old_password"], b["new_password"])
    return 0, "密码已修改", {"affected": aff}


# ---------- 扩展路由表：(method, pattern, handler, perm, param_names) ----------
EXT_ROUTES: List[Tuple[str, str, Callable, str, List[str]]] = [
    # 报刊数据管理
    ("GET",    r"^/api/newspapers$",                     ext_list_newspapers,  "newspaper:list",   []),
    ("POST",   r"^/api/newspapers$",                     ext_create_newspaper, "newspaper:create", []),
    ("POST",   r"^/api/newspapers/batch$",               ext_newspaper_batch,  "newspaper:create", []),
    ("GET",    rf"^/api/newspapers/{INT}$",              ext_get_newspaper,    "newspaper:read",   ["id"]),
    ("PUT",    rf"^/api/newspapers/{INT}$",              ext_update_newspaper, "newspaper:update", ["id"]),
    ("PUT",    rf"^/api/newspapers/{INT}/status$",       ext_newspaper_status, "newspaper:update", ["id"]),
    ("PUT",    rf"^/api/newspapers/{INT}/price$",        ext_newspaper_price,  "newspaper:update", ["id"]),
    ("GET",    r"^/api/categories$",                     ext_list_categories,  "category:list",    []),
    ("GET",    r"^/api/categories/tree$",                ext_category_tree,    "category:list",    []),
    ("POST",   r"^/api/categories$",                     ext_add_category,     "category:create",  []),
    ("PUT",    rf"^/api/categories/{INT}$",              ext_update_category,  "category:update",  ["id"]),
    ("DELETE", rf"^/api/categories/{INT}$",              ext_delete_category,  "category:delete",  ["id"]),
    # 订阅管理
    ("GET",    r"^/api/subscriptions$",                  ext_list_subs,          "subscription:list",   []),
    ("POST",   r"^/api/subscriptions$",                  ext_create_sub,         "subscription:create", []),
    ("GET",    r"^/api/subscriptions/expiring$",         ext_sub_expiring,       "subscription:list",   []),
    ("GET",    r"^/api/subscriptions/stat/newspaper$",   ext_sub_stat_newspaper, "subscription:list",   []),
    ("GET",    r"^/api/subscriptions/stat/customer-type$", ext_sub_stat_custtype, "subscription:list",  []),
    ("GET",    r"^/api/subscriptions/stat/period$",      ext_sub_stat_period,    "subscription:list",   []),
    ("GET",    rf"^/api/subscriptions/{INT}$",           ext_get_sub,            "subscription:read",   ["id"]),
    ("POST",   rf"^/api/subscriptions/{INT}/cancel$",    ext_cancel_sub,         "subscription:update", ["id"]),
    ("POST",   rf"^/api/subscriptions/{INT}/change$",    ext_change_sub,         "subscription:update", ["id"]),
    # 报刊入库管理
    ("POST",   r"^/api/stock/in$",                       ext_stock_in,        "stock:in",     []),
    ("POST",   r"^/api/stock/in/batch$",                 ext_stock_in_batch,  "stock:in",     []),
    ("GET",    r"^/api/stock/in$",                       ext_list_stock_in,   "stock:list",   []),
    ("GET",    r"^/api/stock/warning$",                  ext_stock_warning,   "stock:list",   []),
    ("PUT",    r"^/api/stock/threshold$",                ext_stock_threshold, "stock:update", []),
    ("POST",   r"^/api/stock/check$",                    ext_stock_check,     "stock:check",  []),
    ("GET",    r"^/api/stock/check$",                    ext_list_checks,     "stock:list",   []),
    ("GET",    r"^/api/stock$",                          ext_list_stock,      "stock:list",   []),
    # 报刊发放系统
    ("POST",   r"^/api/delivery/tasks/generate$",        ext_gen_tasks,     "delivery:generate", []),
    ("POST",   r"^/api/delivery/tasks/assign$",          ext_assign_tasks,  "delivery:assign",   []),
    ("GET",    r"^/api/delivery/tasks$",                 ext_list_tasks,    "delivery:list",     []),
    ("POST",   rf"^/api/delivery/tasks/{INT}/sign$",     ext_sign_task,     "sign:confirm",      ["id"]),
    ("POST",   rf"^/api/delivery/tasks/{INT}/abnormal$", ext_abnormal_task, "delivery:update",   ["id"]),
    ("GET",    r"^/api/delivery/missing$",               ext_list_missing,  "delivery:list",     []),
    ("POST",   r"^/api/delivery/missing$",               ext_report_missing, "delivery:update",  []),
    ("POST",   rf"^/api/delivery/missing/{INT}/reissue$", ext_reissue,      "delivery:update",   ["id"]),
    # 系统用户管理
    ("GET",    r"^/api/employees$",                            ext_list_employees,   "employee:list",   []),
    ("POST",   r"^/api/employees$",                            ext_add_employee,     "employee:create", []),
    ("GET",    rf"^/api/employees/{INT}$",                     ext_get_employee,     "employee:read",   ["id"]),
    ("PUT",    rf"^/api/employees/{INT}$",                     ext_update_employee,  "employee:update", ["id"]),
    ("DELETE", rf"^/api/employees/{INT}$",                     ext_delete_employee,  "employee:delete", ["id"]),
    ("PUT",    rf"^/api/employees/{INT}/status$",              ext_employee_status,  "employee:update", ["id"]),
    ("PUT",    rf"^/api/employees/{INT}/password$",            ext_employee_password, "employee:update", ["id"]),
    ("GET",    rf"^/api/employees/{INT}/levels$",              ext_employee_levels,  "employee:read",   ["id"]),
    ("POST",   rf"^/api/employees/{INT}/levels$",              ext_assign_level,     "employee:update", ["id"]),
    ("DELETE", rf"^/api/employees/{INT}/levels/{NAME}$",       ext_revoke_level,     "employee:update", ["id", "level"]),
    ("GET",    rf"^/api/employees/{INT}/permissions$",         ext_employee_perms,   "employee:read",   ["id"]),
    ("POST",   rf"^/api/employees/{INT}/permissions$",         ext_grant_perm,       "employee:update", ["id"]),
    ("DELETE", rf"^/api/employees/{INT}/permissions/{NAME}$",  ext_revoke_perm,      "employee:update", ["id", "code"]),
    ("GET",    r"^/api/auth-levels$",                          ext_list_levels,      "authlevel:list",   []),
    ("POST",   r"^/api/auth-levels$",                          ext_create_level,     "authlevel:create", []),
    ("GET",    r"^/api/logs$",                                 ext_list_logs,        "log:list",         []),
    # 个人中心（perm 为空 = 任意登录员工可访问，仅作用于本人）
    ("GET",    r"^/api/profile$",                              ext_profile,          "", []),
    ("PUT",    r"^/api/profile/password$",                     ext_change_pwd,       "", []),
]


def _match_ext(method: str, path: str) -> Optional[_RouteMatch]:
    """扩展路由匹配：按 param_names 顺序把捕获组映射成具名参数。"""
    for m, pattern, handler, perm, names in EXT_ROUTES:
        if m != method:
            continue
        mo = re.match(pattern, path)
        if not mo:
            continue
        groups = mo.groups()
        params: Dict[str, Any] = {}
        for i, g in enumerate(groups):
            key = names[i] if i < len(names) else f"g{i}"
            params[key] = g
        rm = _RouteMatch()
        rm.handler = handler
        rm.perm = perm
        rm.params = params
        return rm
    return None


class ApiHandler(BaseHTTPRequestHandler):

    """RESTful 请求分发器。"""

    # 使用 HTTP/1.1 以正确支持浏览器 keep-alive 连接（配合 _json 的 Content-Length），
    # 否则浏览器（Chrome/Edge）会报 ERR_INVALID_HTTP_RESPONSE。
    protocol_version = "HTTP/1.1"

    # 命令行启动时不打印默认日志，改用 logger
    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)

    # ---------- 公共流程 ----------
    operator_id: Optional[int] = None  # 每请求由 _prepare 填充

    def _prepare(self) -> Optional[Tuple[int, str, Any]]:
        """
        解析请求头 / 路径 / body，返回 None 表示继续，
        否则返回 (code, msg, data) 直接写给客户端（用于早期失败）。
        同时把 operator_id 挂到 self。
        """
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        # 解析操作者
        emp_id_raw = self.headers.get("X-Emp-Id")
        self.operator_id = _int(emp_id_raw, 0) or None

        # 读 body（仅非 GET）
        body: Dict[str, Any] = {}
        if self.command in ("POST", "PUT", "PATCH", "DELETE"):
            length = _int(self.headers.get("Content-Length"), 0)
            if length > 0:
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw.decode("utf-8"))
                    if not isinstance(body, dict):
                        body = {}
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return 400, "请求体不是合法 JSON", None
        self._path = path
        self._query = query
        self._body = body
        return None

    def _dispatch(self, method: str):
        # CORS 预检：必须先 send_response（发状态行+重置头缓冲），再 send_header，
        # 否则头会丢失；HTTP/1.1 keep-alive 下 204 需显式 Content-Length:0。
        if method == "OPTIONS":
            self.send_response(204)
            self._send_cors()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        early = self._prepare()
        if early:
            self._json(*early)
            return

        # 登录为公开接口，不需要 X-Emp-Id，优先处理
        if method == "POST" and self._path == "/api/login":
            code, msg, data = _do_login(self._body)
            http_status = 200 if code == 0 else code
            self._json(code, msg, data, http_status=http_status)
            return

        match = _match_route(method, self._path)
        if not match:
            match = _match_ext(method, self._path)
        if not match:
            self._json(404, f"无匹配路由: {method} {self._path}", None, http_status=404)
            return

        # ---------- 权限校验 ----------
        if self.operator_id is None or self.operator_id <= 0:
            self._json(401, "缺少 X-Emp-Id 操作者标识", None, http_status=401)
            return
        # perm 为空串表示「任意登录员工可访问」（如个人中心），跳过接口权限校验
        if match.perm and not PermissionChecker.check_api(self.operator_id, match.perm):
            logger.warning("越权: emp_id=%s 需要 api:%s",
                           self.operator_id, match.perm)
            self._json(403, f"无权限: 需要 api:{match.perm}", None, http_status=403)
            return

        # ---------- 执行业务 ----------
        try:
            code, msg, data = match.handler(
                self, match.params, self._query, self._body)
        except ValueError as e:       # 业务参数校验
            self._json(400, str(e), None, http_status=400)
            return
        except PermissionError as e:  # 业务层越权
            self._json(403, str(e), None, http_status=403)
            return
        except Exception as e:        # 兜底
            logger.exception("接口异常")
            self._json(500, f"内部错误: {e}", None, http_status=500)
            return

        # 业务码映射 HTTP 状态（404/403 已在 handler 内返回对应 code）
        http_status = 200 if code == 0 else code
        self._json(code, msg, data, http_status=http_status)

    # ---------- HTTP 动词 ----------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    # ---------- 响应工具 ----------
    def _send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods",
                         "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type,X-Emp-Id")

    def _json(self, code: int, msg: str, data: Any,
              http_status: int = 200):
        payload = {"code": code, "msg": msg, "data": data}
        body = json.dumps(payload, ensure_ascii=False,
                          default=str).encode("utf-8")
        self.send_response(http_status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass


# ================================================================
# 五、启动入口
# ================================================================
def _init_all_tables() -> None:
    """启动前初始化全部模块的表结构（幂等）。"""
    init_database()                 # 权限/员工/日志（含标准权限等级）
    init_tables()                   # 客户数据管理
    newspaper_mod.init_tables()     # 报刊数据管理
    subscription_mod.init_tables()  # 订阅管理
    stock_mod.init_tables()         # 报刊入库管理
    delivery_mod.init_tables()      # 报刊发放系统


def run(port: int = 8088, host: str = "0.0.0.0") -> None:
    # 启动前确保所有模块相关表已建好
    try:
        _init_all_tables()
    except Exception as e:
        logger.error("建表失败（请检查 MySQL 是否启动 / DB_CONFIG 配置）: %s", e)
    server = ThreadingHTTPServer((host, port), ApiHandler)
    logger.info("邮局报刊订阅系统 API 已启动: http://%s:%s", host, port)
    logger.info("调用示例：")
    logger.info('  curl -H "X-Emp-Id: 1" "http://127.0.0.1:%s/api/customers"', port)
    logger.info('  curl -H "X-Emp-Id: 1" -H "Content-Type: application/json" '
                '-X POST -d \'{"cust_no":"C001","cust_type":"personal",'
                '"name":"张三","phone":"13800000000"}\' '
                '"http://127.0.0.1:%s/api/customers"', port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("服务已停止。")
    finally:
        server.server_close()


def main() -> None:
    port = 8088
    if len(sys.argv) > 1:
        port = _int(sys.argv[1], 8088)
    run(port=port)


if __name__ == "__main__":
    main()
