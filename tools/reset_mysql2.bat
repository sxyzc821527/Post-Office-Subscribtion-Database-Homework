@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
REM ================================================================
REM  MySQL 8.0 root 密码重置脚本 v2（写 my.ini 方式，更可靠）
REM  方法：在 my.ini 的 [mysqld] 段加 skip-grant-tables，重启 MySQL80，
REM        免密登录改密，再删掉该行、重启恢复。
REM  使用：右键 -> 以管理员身份运行
REM ================================================================

set "INIFILE=C:\ProgramData\MySQL\MySQL Server 8.0\my.ini"
set "MYSQL=C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe"
set "SVCNAME=MySQL80"
set "MARKER=# >>>PO_RESET_TEMP<<<"

echo.
echo ===== MySQL root 密码重置 v2 =====
echo.

REM ---- 管理员检查 ----
net session >nul 2>&1
if errorlevel 1 (
    echo [错误] 当前不是管理员！请右键此脚本 -> 以管理员身份运行。
    pause
    exit /b 1
)

REM ---- 备份 my.ini ----
if not exist "%INIFILE%.po_bak" copy /Y "%INIFILE%" "%INIFILE%.po_bak" >nul
echo [OK] 已备份 my.ini -> %INIFILE%.po_bak

REM ---- 清理可能残留的标记行（幂等）----
findstr /v /c:"# >>>PO_RESET_TEMP<<<" "%INIFILE%" > "%INIFILE%.tmp"
move /Y "%INIFILE%.tmp" "%INIFILE%" >nul

REM ---- 输入新密码 ----
set /p "NEWPWD=请输入新的 root 密码（不含空格/引号/特殊符号）: "
if "!NEWPWD!"=="" (
    echo [错误] 密码不能为空。
    pause
    exit /b 1
)

echo.
echo [1/6] 在 my.ini [mysqld] 段追加 skip-grant-tables ...
REM 在 [mysqld] 行后插入：marker + skip-grant-tables + marker，便于回删。保持原编码(UTF-8 无 BOM)。
powershell -NoProfile -ExecutionPolicy Bypass -Command "$f='%INIFILE%'; $lines=[System.IO.File]::ReadAllLines($f,[System.Text.Encoding]::UTF8); $out=New-Object System.Collections.Generic.List[string]; foreach($l in $lines){ $out.Add($l); if($l.Trim() -eq '[mysqld]'){ $out.Add('# >>>PO_RESET_TEMP<<<'); $out.Add('skip-grant-tables'); $out.Add('# >>>PO_RESET_TEMP<<<'); } }; [System.IO.File]::WriteAllLines($f,$out,[System.Text.UTF8Encoding]::new($false))" >nul 2>&1
if errorlevel 1 (
    echo [错误] 写入 my.ini 失败，正在回滚...
    move /Y "%INIFILE%.po_bak" "%INIFILE%" >nul
    pause
    exit /b 1
)

echo.
echo [2/6] 重启 MySQL 服务 %SVCNAME%（载入 skip-grant-tables）...
net stop %SVCNAME%
net start %SVCNAME%
echo （等待服务就绪...）
timeout /t 5 >nul

echo.
echo [3/6] 免密连接并修改 root 密码 ...
REM skip-grant-tables 模式：先用 socket/本地连，FLUSH PRIVILEGES 后才能 ALTER
"%MYSQL%" -u root --skip-password -e "FLUSH PRIVILEGES; ALTER USER 'root'@'localhost' IDENTIFIED WITH mysql_native_password BY '!NEWPWD!'; FLUSH PRIVILEGES;"
if errorlevel 1 (
    echo [警告] mysql_native_password 失败，尝试 caching_sha2_password ...
    "%MYSQL%" -u root --skip-password -e "FLUSH PRIVILEGES; ALTER USER 'root'@'localhost' IDENTIFIED BY '!NEWPWD!'; FLUSH PRIVILEGES;"
    if errorlevel 1 (
        echo [错误] 两种方式都失败。下面会恢复 my.ini 并重启服务。
        goto RESTORE
    )
)

echo.
echo [4/6] 密码修改成功。移除 my.ini 中的 skip-grant-tables ...
:RESTORE
findstr /v /c:"# >>>PO_RESET_TEMP<<<" "%INIFILE%" | findstr /v /c:"skip-grant-tables" > "%INIFILE%.tmp2"
move /Y "%INIFILE%.tmp2" "%INIFILE%" >nul

echo.
echo [5/6] 重启 MySQL 服务 %SVCNAME%（恢复正常模式）...
net stop %SVCNAME%
net start %SVCNAME%

echo.
echo [6/6] 用新密码验证连接 ...
timeout /t 3 >nul
"%MYSQL%" -u root -p"!NEWPWD!" -e "SELECT 'CONNECTION_OK' AS result;"
if errorlevel 1 (
    echo [警告] 新密码连接验证失败。可能是密码改没成功，或认证插件问题。
    echo        my.ini 已恢复，MySQL80 已重启（正常模式）。
    echo        备份文件：%INIFILE%.po_bak
    pause
    exit /b 1
)

echo.
echo ===== 重置完成 =====
echo root 新密码已生效。请用此密码启动 run_server.py。
echo.
pause
endlocal
