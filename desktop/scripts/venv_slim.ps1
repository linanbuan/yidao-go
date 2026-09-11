# venv_slim.ps1 — 切除 PySide6 全家桶里弈道用不到的附加模块。
#
# 背景：PySide6 元发行包含 QtWebEngine（194MB dll + 101MB 资源 + 44MB 语言包）、
# QML/Quick/3D/Designer/Pdf/Graphs 等一大堆模块，而弈道桌面端只 import 了
# QtCore/QtGui/QtWidgets/QtNetwork/QtTest/QtMultimedia/QtCharts 这七个模块。
# 本脚本按「白名单」删除其余装饰（dll/pyd/pyi/插件/qml/翻译/开发元数据），
# 立方体约砍掉 450MB。幂等：已删路径自动跳过，重建 venv 后可重复运行。
#
# 用法：项目根目录下  powershell -ExecutionPolicy Bypass -File desktop\scripts\venv_slim.ps1
# 验证：desktop\tests 全套（QtCharts/QtMultimedia/QtNetwork 都在保留名单里）。

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent          # desktop\
$ps = Join-Path $root '.venv\Lib\site-packages\PySide6'
if (-not (Test-Path $ps)) { Write-Host "未找到 PySide6：$ps"; exit 1 }

# —— 保留清单（弈道实际 import 的模块 + Qt 基础运行时）——
$keep = @(
  'Core','Gui','Widgets','Network','WebSockets','OpenGL','OpenGLWidgets',
  'Test','Concurrent','DBus','Xml','PrintSupport',
  'Charts','Multimedia','MultimediaWidgets','Svg','SvgWidgets','Sql'
)
# 备注：WebSockets 是 `core/ws.py` 在用的（QWebSocket），曾因扫描漏网被误删过一次（§5-L84），
# 恢复后已进白名单；下次重建 venv 必须保留它。

function Get-DirSize([string]$p) {
  $s = 0
  Get-ChildItem $p -Force -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.PSIsContainer) { $s += (Get-ChildItem $_.FullName -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum }
    else { $s += $_.Length }
  }
  return $s
}

$before = (Get-DirSize $ps) / 1MB

# 1) 整目录：WebEngine 运行时资源 / QML 模块树 / 开发元数据 / 用不到的插件
$dirs = @(
  'resources',                       # ~101MB：icudtl.dat、v8 快照、devtools pak——全是 WebEngine 的
  'qml',                             # ~25MB：QtQuick/QtQml 模块树（弈道是纯 Widgets 应用）
  'metatypes',                       # ~15MB：QML/工具链用的开发元数据，运行时不读
  'typesystems',                     # shiboken 开发用
  'translations\qtwebengine_locales',# ~44MB：WebEngine 各语言 .pak
  # plugins 下用不到的：SQL/场景/资源导入/QML/设计器/总线/地理/语音等
  'plugins\sqldrivers','plugins\sceneparsers','plugins\assetimporters','plugins\renderers',
  'plugins\qmltooling','plugins\designer','plugins\canbus','plugins\geoservices',
  'plugins\texttospeech','plugins\webview','plugins\position','plugins\geometryloaders',
  'plugins\qmllint','plugins\scxmldatamodel','plugins\sensors','plugins\renderplugins',
  'plugins\vectorimageformats'
)
foreach ($d in $dirs) {
  $p = Join-Path $ps $d
  if (Test-Path $p) { Remove-Item $p -Recurse -Force; Write-Host "删目录  $d" }
}

# 2) dll / pyd / pyi：名字剥掉 Qt6/Qt 前缀后不在白名单的，全删
$n = 0
Get-ChildItem $ps -File | Where-Object {
  $_.Extension -in '.dll','.pyd','.pyi' -and
  $_.Name -match '^Qt6?[A-Z]' -and
  ($_.BaseName -replace '^Qt6?','') -notin $keep -and
  $_.BaseName -notmatch '^__'
} | ForEach-Object { Remove-Item $_.FullName -Force; $n++; Write-Host "删文件  $($_.Name)" }
Write-Host "文件级删除 $n 个"

# 3) 翻译只留简体中文（其余 ~14MB 的 qtbase_*.qm / designer_*.qm 用不上）
Get-ChildItem (Join-Path $ps 'translations') -Filter '*.qm' -File |
  Where-Object { $_.BaseName -notin @('qtbase_zh_CN','qt_zh_CN') } | Remove-Item -Force

# 4) 扫尾：任何漏网的 WebEngine/webengine 文件
Get-ChildItem $ps -Recurse -File -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -match 'WebEngine|webengine' } | Remove-Item -Force

$after = (Get-DirSize $ps) / 1MB
Write-Host ("PySide6：{0:N1} MB → {1:N1} MB（省 {2:N1} MB）" -f $before, $after, ($before - $after))

# 5) 收尾自检：把全项目实际 import 的模块逐个拉一遍，缺了立刻报红（曾漏检 QtWebSockets，教训见 §5-L84）
$py = Join-Path $root '.venv\Scripts\python.exe'
& $py -c "from PySide6 import QtCore, QtGui, QtWidgets, QtNetwork, QtWebSockets, QtMultimedia, QtCharts, QtTest; print('冒烟 import 全过')"
if ($LASTEXITCODE -ne 0) { Write-Host '!!! 冒烟 import 失败：有保留清单外的模块被误删，先 pip 装回再重跑本脚本'; exit 1 }