# setup_env.ps1 —— 新机器/新克隆的环境引导：建两个 venv 并装依赖。
#
# ⚠ 本文件**必须保存为「UTF-8 带 BOM」**。Windows PowerShell 5.1 读 `.ps1` 时，
#   无 BOM 的 UTF-8 会按本地 ANSI 代码页解，下面的中文注释直接变成乱码并把引号配对
#   搞乱 —— 表现是 6 处莫名其妙的语法错（「语句块缺少右 }」等）。同目录的
#   `desktop/scripts/venv_slim.ps1` 也带 BOM，就是同一个原因。改完请复核前三字节
#   是 EF BB BF。
#
# 为什么需要它（审计 P0 第 1 条）：`启动围棋平台.bat` 只检查 `desktop\.venv`
# 是否存在，缺失时提示「run the setup step first」——而这个 setup step 原先藏在
# 已被删除的图形启动器里（PACKAGING.md 曾写明「启动器负责自动建 venv 并装依赖」）。
# 启动器重做后不再承担环境引导，于是新机器上双击 .bat 只会得到一句无解的提示。
# 本脚本把这一步补成一条可复制的命令。
#
# 用法（仓库根目录下）：
#     powershell -ExecutionPolicy Bypass -File scripts\setup_env.ps1
#
#     -Force        已有 venv 也重建
#     -SkipSlim     不跑 PySide6 瘦身（保留官方完整包，约 +450MB）
#     -IndexUrl     指定 pip 源（默认读环境变量 PIP_INDEX_URL；受限网络下用得上）
#     -Python      指定解释器路径（默认自动探测 3.12 / 3.11 / 3.13）
#
# 两个 venv 是刻意分开的：`backend\.venv` 只要后端依赖（容器与 CI 同口径），
# `desktop\.venv` 要能 import 整个后端**加上** PySide6（桌面端把后端内嵌在同进程里，
# 见 desktop/core/backend_host.py）。塞进同一个 venv 会让后端镜像白白胖胖。

param(
  [switch]$Force,
  [switch]$SkipSlim,
  [string]$IndexUrl = $env:PIP_INDEX_URL,
  [string]$Python = ""
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
Write-Host "项目根目录：$root"

# ---------------------------------------------------------------------------
# 1. 找一个可用的解释器（3.11~3.13；本仓库实测 3.12.10）
# ---------------------------------------------------------------------------
function Resolve-Python {
  param([string]$Explicit)
  if ($Explicit) {
    if (-not (Test-Path $Explicit)) { throw "指定的解释器不存在：$Explicit" }
    return $Explicit
  }
  foreach ($cand in @('py -3.12', 'py -3.11', 'py -3.13', 'python', 'python3')) {
    $parts = $cand.Split(' ')
    $exe = $parts[0]
    $cmd = Get-Command $exe -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    try {
      $args = @()
      if ($parts.Length -gt 1) { $args += $parts[1] }
      $args += @('-c', 'import sys;print(sys.executable)')
      $p = & $exe @args 2>$null
      if ($LASTEXITCODE -eq 0 -and $p) {
        $v = & $exe @($args[0..($args.Length-3)]) '-c' 'import sys;print("%d.%d"%sys.version_info[:2])' 2>$null
        Write-Host "找到解释器：$p（Python $v）"
        return $p.Trim()
      }
    } catch { continue }
  }
  throw "没有找到可用的 Python。请先安装 Python 3.12（勾选 Add to PATH），或用 -Python 指定路径。"
}

$py = Resolve-Python -Explicit $Python

# 版本闸门：3.11 以下 `list[tuple[...]]` 之类的语法与 sqlalchemy 2.x 都不保证能用
$ver = & $py -c "import sys;print('%d.%d'%sys.version_info[:2])"
if ([version]$ver -lt [version]'3.11') { throw "需要 Python 3.11+，当前 $ver" }

$pipArgs = @()
if ($IndexUrl) { $pipArgs = @('-i', $IndexUrl); Write-Host "pip 源：$IndexUrl" }

# ---------------------------------------------------------------------------
# 2. 建 venv 的公共流程
# ---------------------------------------------------------------------------
function New-Venv {
  param([string]$Dir, [string]$ReqFile, [string]$Label)
  $pyExe = Join-Path $Dir 'Scripts\python.exe'
  if ((Test-Path $pyExe) -and -not $Force) {
    Write-Host "`n[$Label] 已有 venv，跳过创建（-Force 可重建）：$Dir"
  } else {
    if (Test-Path $Dir) { Remove-Item $Dir -Recurse -Force }
    Write-Host "`n[$Label] 创建 venv：$Dir"
    & $py -m venv $Dir
    if ($LASTEXITCODE -ne 0) { throw "[$Label] venv 创建失败" }
  }
  Write-Host "[$Label] 安装依赖：$ReqFile"
  & $pyExe -m pip install --upgrade pip --quiet @pipArgs
  & $pyExe -m pip install -r $ReqFile @pipArgs
  if ($LASTEXITCODE -ne 0) { throw "[$Label] 依赖安装失败（受限网络下试试 -IndexUrl <镜像源>）" }
  return $pyExe
}

# ---------------------------------------------------------------------------
# 3. 后端 venv：只装后端依赖（含 pytest，走 requirements-dev.txt）
# ---------------------------------------------------------------------------
$backendPy = New-Venv -Dir (Join-Path $root 'backend\.venv') `
  -ReqFile (Join-Path $root 'backend\requirements-dev.txt') -Label 'backend'

# ---------------------------------------------------------------------------
# 4. 桌面 venv：后端依赖 + PySide6（内嵌后端要求同解释器能 import app.*）
# ---------------------------------------------------------------------------
$desktopPy = New-Venv -Dir (Join-Path $root 'desktop\.venv') `
  -ReqFile (Join-Path $root 'desktop\requirements-dev.txt') -Label 'desktop'

# ---------------------------------------------------------------------------
# 5. PySide6 瘦身（可选）：官方 wheel 带 WebEngine/QML 等一堆用不到的模块，
#    砍掉约 450MB。脚本自带 import 冒烟，误删会自己报红。
# ---------------------------------------------------------------------------
if (-not $SkipSlim) {
  Write-Host "`n[桌面] PySide6 瘦身"
  & powershell -ExecutionPolicy Bypass -File (Join-Path $root 'desktop\scripts\venv_slim.ps1')
  if ($LASTEXITCODE -ne 0) { Write-Host "瘦身未通过自检，已跳过（不影响可用性，只是占空间）" }
} else {
  Write-Host "`n[桌面] 已跳过 PySide6 瘦身（-SkipSlim）"
}

# ---------------------------------------------------------------------------
# 6. 收尾自检：两个解释器各自把关键 import 拉一遍
# ---------------------------------------------------------------------------
Write-Host "`n===== 自检 ====="
& $backendPy -c "import fastapi, sqlalchemy, httpx, jwt, app.main; print('后端 import 通过')"
if ($LASTEXITCODE -ne 0) { throw "后端 import 失败" }

& $desktopPy -c "import PySide6, fastapi, app.main; from PySide6 import QtCharts, QtWebSockets, QtMultimedia; print('桌面 import 通过（含 QtCharts/QtWebSockets/QtMultimedia）')"
if ($LASTEXITCODE -ne 0) { throw "桌面 import 失败" }

Write-Host "`n环境就绪。双击「启动围棋平台.bat」即可运行；"
Write-Host "跑测试："
Write-Host "  cd backend; .\.venv\Scripts\python.exe -m pytest tests"
Write-Host "  cd desktop; .\.venv\Scripts\python.exe -m pytest tests"
