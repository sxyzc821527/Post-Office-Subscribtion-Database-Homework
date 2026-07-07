# -*- coding: utf-8 -*-
"""
================================================================
一键启动脚本（本地开发用）
================================================================
功能：
    1. 若未设置 PO_DB_PASSWORD 环境变量，则用 getpass 安全提示输入
       MySQL 口令（终端隐藏输入，不回显、不落盘）。
    2. 自动创建数据库 post_office（若不存在）。
    3. 建好全部表并写入标准权限等级 + 初始 admin 账号。
    4. 启动 HTTP API 服务（默认 8088）。

用法：
    python run_server.py            # 默认端口 8088
    python run_server.py 9000       # 自定义端口

数据库连接可用环境变量覆盖：
    PO_DB_HOST / PO_DB_PORT / PO_DB_USER / PO_DB_PASSWORD / PO_DB_NAME
================================================================
"""

import getpass
import os
import sys

import pymysql

_HERE = os.path.dirname(os.path.abspath(__file__))


def ensure_password() -> None:
    """确保 PO_DB_PASSWORD 已就绪；优先级：环境变量 > 本地 .db_pwd 文件 > getpass 输入。"""
    if os.environ.get("PO_DB_PASSWORD") is not None:
        return
    pwd_file = os.path.join(_HERE, ".db_pwd")
    if os.path.exists(pwd_file):
        try:
            with open(pwd_file, "r", encoding="utf-8") as f:
                pwd = f.read().strip()
            if pwd:
                os.environ["PO_DB_PASSWORD"] = pwd
                return
        except OSError:
            pass
    user = os.environ.get("PO_DB_USER", "root")
    pwd = getpass.getpass(f"请输入 MySQL 用户 {user} 的密码（输入不显示）：")
    os.environ["PO_DB_PASSWORD"] = pwd


def ensure_database() -> None:
    """连接 MySQL（不指定库），创建 post_office 数据库。"""
    host = os.environ.get("PO_DB_HOST", "127.0.0.1")
    port = int(os.environ.get("PO_DB_PORT", "3306"))
    user = os.environ.get("PO_DB_USER", "root")
    pwd = os.environ.get("PO_DB_PASSWORD", "")
    dbname = os.environ.get("PO_DB_NAME", "post_office")
    conn = pymysql.connect(host=host, port=port, user=user, password=pwd,
                           charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{dbname}` "
                f"DEFAULT CHARACTER SET utf8mb4 DEFAULT COLLATE utf8mb4_general_ci")
        conn.commit()
        print(f"[OK] 数据库 {dbname} 就绪。")
    finally:
        conn.close()


def ensure_admin() -> None:
    """写入初始 admin 账号（若不存在），并分配 O5 超级管理员。"""
    from server.core.authority import (EmployeeService, PermissionService,  # noqa: E402
                                       hash_password, get_conn)
    if EmployeeService.get_employee_by_username("admin"):
        return
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sys_employee(emp_no, username, password, real_name, status) "
            "VALUES(%s,%s,%s,%s,1)",
            ("ADMIN", "admin", hash_password("admin123"), "系统管理员"),
        )
        conn.commit()
        admin_id = cur.lastrowid
    PermissionService.assign_level(admin_id, "O5")
    print("[OK] 初始账号已创建：admin / admin123（O5 超级管理员）。")


def main() -> None:
    port = 8088
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass

    ensure_password()
    try:
        ensure_database()
    except pymysql.err.OperationalError as e:
        code = e.args[0] if e.args else 0
        if code in (2003, 2002):
            print("[ERR] 无法连接 MySQL 服务。请确认：")
            print("      1) 本机已安装并启动 MySQL 服务（MySQL80）")
            print("      2) 端口 3306 可用")
            print(f"      详细错误：{e}")
        elif code == 1045:
            print("[ERR] MySQL 用户名或密码错误。")
            print("      可删除本目录下的 .db_pwd 文件后重试，重新输入密码；")
            print("      或用 tools/reset_mysql2.bat 重置 root 密码。")
            print(f"      详细错误：{e}")
        else:
            print(f"[ERR] 数据库连接失败：{e}")
        sys.exit(1)
    except Exception as e:
        print(f"[ERR] 无法连接/创建数据库，请检查 MySQL 与口令：{e}")
        sys.exit(1)

    # 建表 + 启动服务（api.run 内部会 _init_all_tables）
    from server import api  # noqa: E402
    try:
        api._init_all_tables()
    except Exception as e:
        print(f"[ERR] 建表失败：{e}")
        sys.exit(1)
    ensure_admin()
    print(f"[OK] 启动 API 服务：http://127.0.0.1:{port}  （Ctrl+C 停止）")
    api.run(port=port)


if __name__ == "__main__":
    main()
