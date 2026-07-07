@echo off
REM ================================================================
REM  MySQL 8.0 root 密码重置脚本（Windows）
REM  使用方法：右键 -> 以管理员身份运行
REM  本脚本自动完成：停服务 -> 免密启动 -> 改密 -> 恢复服务
REM ================================================================
chcp 65001 >nul
setlocal

echo.
echo ===== MySQL root 密码重置 =====
echo.

REM ---- 配置（如路径不同请修改）----
set "MYSQLD=C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqld.exe"
set "MYSQL=C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe"
set "INIFILE=C:\ProgramData\MySQL\MySQL Server 8.0\my.ini"
set "SVCNAME=MySQL80"

REM ---- 检查管理员权限 ----
net session >nul 2>&1
if errorlevel 1 (
    echo [错误] 当前不是管理员！请右键此脚本，选择「以管理员身份运行」。
    pause
    exit /b 1
)

REM ---- 输入新密码 ----
set /p "NEWPWD=请输入新的 root 密码（建议不含空格和引号）: "
if "%NEWPWD%"=="" (
    echo [错误] 密码不能为空。
    pause
    exit /b 1
)

echo.
echo [1/5] 停止 MySQL 服务 %SVCNAME% ...
net stop %SVCNAME%
if errorlevel 1 (
    echo [警告] 服务停止失败或已停止，继续尝试...
)

echo.
echo [2/5] 以 --skip-grant-tables 免密模式启动临时实例 ...
REM 注册独立临时服务 MySqlSkip，使用 3307 端口，避免与原服务端口冲突
"%MYSQLD%" --install MySqlSkip --defaults-file="%INIFILE%" --skip-grant-tables --port=3307
net start MySqlSkip
echo （等待临时实例就绪...）
timeout /t 4 >nul

echo.
echo [3/5] 执行密码修改 ...
REM 通过 3307 端口连临时实例；先 FLUSH 再 ALTER USER
"%MYSQL%" -h 127.0.0.1 -P 3307 -u root --skip-password -e "FLUSH PRIVILEGES; ALTER USER 'root'@'localhost' IDENTIFIED WITH mysql_native_password BY '%NEWPWD%'; FLUSH PRIVILEGES;"
if errorlevel 1 (
    echo [错误] 密码修改失败，下面尝试用 caching_sha2_password 插件重试...
    "%MYSQL%" -h 127.0.0.1 -P 3307 -u root --skip-password -e "FLUSH PRIVILEGES; ALTER USER 'root'@'localhost' IDENTIFIED BY '%NEWPWD%'; FLUSH PRIVILEGES;"
    if errorlevel 1 (
        echo [错误] 两种方式都失败。正在恢复原服务，请检查上面报错。
        net stop MySqlSkip
        sc delete MySqlSkip
        net start %SVCNAME%
        pause
        exit /b 1
    )
)

echo.
echo [4/5] 停止临时实例并清理 ...
net stop MySqlSkip
sc delete MySqlSkip

echo.
echo [5/5] 恢复 MySQL 服务 %SVCNAME% ...
net start %SVCNAME%
if errorlevel 1 (
    echo [警告] 服务恢复失败，请手动在「服务」里启动 MySQL80。
    pause
    exit /b 1
)

echo.
echo ===== 重置完成 =====
echo 新密码已设为：%NEWPWD%
echo 请用此密码启动 run_server.py。
echo.
pause
endlocal
