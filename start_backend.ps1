# 启动后端（FastAPI + KataGo 引擎池 + 复盘 worker）
# 用法：在项目根目录执行  .\start_backend.ps1
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$backend = Join-Path $root "backend"
$venvPython = Join-Path $backend ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "[setup] 未找到虚拟环境，正在创建 backend\.venv ..." -ForegroundColor Yellow
    python -m venv (Join-Path $backend ".venv")
}

Write-Host "[setup] 安装/更新依赖（使用清华镜像，可按需修改）..." -ForegroundColor Yellow
& $venvPython -m pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r (Join-Path $backend "requirements.txt")

$katagoExe = Join-Path $backend "katago\katago.exe"
if (-not (Test-Path $katagoExe)) {
    Write-Host "[warn] 未检测到 KataGo，将使用内置启发式引擎（棋力与分析精度有限）。" -ForegroundColor Yellow
    Write-Host "       安装强引擎：cd backend; python katago\download.py" -ForegroundColor Yellow
}

Write-Host "[run] 启动后端 http://127.0.0.1:8000  （API 文档 /docs）" -ForegroundColor Green
Set-Location $backend
# 只听回环：本机访问足够，且避免把 /docs 暴露到局域网（容器里要对外请改 Dockerfile）
& $venvPython -m uvicorn app.main:app --host 127.0.0.1 --port 8000
