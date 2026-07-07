# ===== Post Office Subscription System - One-click Start =====
# Launches: backend API (8088) + frontend web (8000) + browser.
# Close the two popped-up windows to stop services.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$root = $PSScriptRoot
$bePort = 8088
$fePort = 8000

# ---- Auto-detect Python (resolve to full exe path) ----
$py = $null
$pyCmd = Get-Command py -ErrorAction SilentlyContinue
if ($pyCmd) {
    $exePath = (& py -3 -c "import sys; print(sys.executable)") 2>$null
    if ($LASTEXITCODE -eq 0 -and $exePath) { $py = $exePath.Trim() }
}
if (-not $py) {
    $pyCmd2 = Get-Command python -ErrorAction SilentlyContinue
    if ($pyCmd2) {
        $exePath2 = (& python -c "import sys; print(sys.executable)") 2>$null
        if ($LASTEXITCODE -eq 0 -and $exePath2) { $py = $exePath2.Trim() }
    }
}
if (-not $py) {
    Write-Host "[ERROR] Python not found! Please install Python 3.10+ from https://www.python.org/"
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Host "[OK] Using Python: $py ($(& $py --version))"

Write-Host "============================================"
Write-Host "   Post Office Subscription System - Start"
Write-Host "============================================"

# Avoid committing the plaintext password file.
if (Test-Path ".git") {
    $gi = Get-Content ".gitignore" -ErrorAction SilentlyContinue
    if (-not ($gi -match "^.db_pwd$")) { Add-Content ".gitignore" ".db_pwd" }
}

# Ask for MySQL password on first run, save to .db_pwd.
if (-not (Test-Path ".db_pwd")) {
    Write-Host "[First run] MySQL root password needed (saved to .db_pwd)."
    $pwdSecure = Read-Host "Enter MySQL root password" -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($pwdSecure)
    $pwdPlain = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    if (-not $pwdPlain) { Write-Host "[Error] Password empty. Run again."; Read-Host "Press Enter to exit"; exit 1 }
    [System.IO.File]::WriteAllText((Join-Path $root ".db_pwd"), $pwdPlain, [System.Text.Encoding]::ASCII)
    Write-Host "[OK] Password saved to .db_pwd."
}

Write-Host "[1/3] Starting backend API (port $bePort) ..."
# Ensure pymysql is installed (auto-install if missing)
& $py -c "import pymysql" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "      pymysql not found, installing ..."
    & $py -m pip install pymysql --quiet
}
Start-Process -FilePath $py -ArgumentList "$root\run_server.py" -WorkingDirectory $root -WindowStyle Normal
Write-Host "      Waiting for backend ..."
Start-Sleep -Seconds 4

Write-Host "[2/3] Starting frontend web (port $fePort) ..."
Start-Process -FilePath $py -ArgumentList "$root\serve_frontend.py" -WorkingDirectory $root -WindowStyle Normal
Start-Sleep -Seconds 2

Write-Host "[3/3] Opening browser ..."
Start-Process "http://127.0.0.1:$fePort/index.html"

Write-Host ""
Write-Host "============================================"
Write-Host "  All started!"
Write-Host "  Frontend: http://127.0.0.1:$fePort/index.html"
Write-Host "  Backend : http://127.0.0.1:$bePort"
Write-Host "  Login   : admin / admin123"
Write-Host "  Stop    : close the two windows (Backend / Frontend)"
Write-Host "============================================"
Write-Host "(You can close this window. Services keep running.)"
Read-Host "Press Enter to close this launcher"