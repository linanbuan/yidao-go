# 桌面客户端封装（PyInstaller）交接文档

**这份文档不是已完成事项，是给"真封装那一轮"的交接单。**
本轮（P5）只做到：把路径口径集中到一处、把 `.ico` 准备好、把已知的会炸点逐个查出来
并钉上测试。`paths.FROZEN` 那条分支**至今没有真跑过一次**，所以下面凡是关于
"封出来会怎样"的句子都标了口径：

- **【实测】** —— 本轮在这台机器上跑过、并且有测试或命令输出作证的；
- **【推断】** —— 从代码/文档读出来的，还没有 exe 可以验。**不要当成事实用**。

工具版本：PySide6 6.11.2 / Python 3.12.10 / Windows 11 25H2。
本机没有装 PyInstaller，所以【推断】那部分一条都没法在本轮转正。

---

## 1. 现在怎么跑（不需要封装）

| 方式 | 命令 | 备注 |
| --- | --- | --- |
| 双击 | `启动围棋平台.bat` | 【实测】用 `desktop\.venv\Scripts\pythonw.exe` 拉起 `launcher\gui.py`。**它不建 venv**：缺环境时打印的就一句「跑 `scripts\setup_env.ps1`」 |
| 启动器窗口 | `python launcher\gui.py` | 玩家版启动器：登录/注册 + 引擎状态 + 装 KataGo + 配大模型；「进入弈道」时先停掉内嵌后端，再拉起客户端（两个 uvicorn 同写一份 SQLite 会撞锁） |
| 直接跑客户端 | `python desktop/app.py`（或 `pythonw desktop/app.py`） | 需要解释器里能 import PySide6 **和**整个后端 |
| 新机器建环境 | `powershell -ExecutionPolicy Bypass -File scripts\setup_env.ps1` | 【实测】建 `backend\.venv` 与 `desktop\.venv` 并装各自依赖；`-Force` 重建、`-SkipSlim` 跳过 PySide6 瘦身、`-IndexUrl` 换源 |

**没有命令行入口了**：第 33 轮删掉的 `launcher/launcher.py` 带走了终端菜单和
`--console / --status / --stop / --desktop` 四个开关，第 34 轮重做的启动器只有 GUI
（`launcher/gui.py`，`main()` 不解析 argv）。任何「用 `--desktop` 直接进客户端」的
旧说明都已作废。

【实测】桌面端用 `desktop\.venv`（PySide6 6.11.2），而 `backend\.venv` **没有**
PySide6 —— 两个环境各自独立，各自装各自的 requirements：
`backend/requirements-dev.txt`（后端 + pytest）与 `desktop/requirements-dev.txt`
（`-r ../backend/requirements.txt` + PySide6 + pytest）。`scripts/setup_env.ps1`
就是按这两份建的。

桌面端把后端**内嵌在同一进程的一个线程里**（`core/backend_host.py`），
所以它不需要先开 8000，也不需要 node/前端：端口是 OS 现分配的随机值，host 固定
`127.0.0.1`。

## 2. 路径的唯一口径：`core/paths.py`

```
FROZEN            sys.frozen 是否存在（PyInstaller 注入）
_BUNDLE           只读资源根：打包后 = sys._MEIPASS，开发期 = desktop/
APP_DIR           安装目录：打包后 = exe 所在目录，开发期 = desktop/
RESOURCES_DIR     _BUNDLE/resources        （音效、app.ico）
data_dir()        GO_DATA_DIR > %LOCALAPPDATA%\Yidao\data > backend/data
ARTIFACTS_DIR     APP_DIR/artifacts        （截图）
LOGS_DIR          APP_DIR/logs             （崩溃日志 client.log）
```

`apply_env()` 必须在 import 后端任何模块之前调用（`app.config` 在 import 时就读
`GO_DATA_DIR`，晚设无效），它同时把 `backend/` 塞进 `sys.path`。
【实测】开发期这条链是通的，桌面全部测试都走它。

**【推断】打包后 `APP_DIR` 那两个可写目录是第一个坑**：装到
`C:\Program Files\Yidao\` 时 `LOGS_DIR`/`ARTIFACTS_DIR` 都在只读区，
`mkdir` 会失败 —— 崩溃日志写不进去就等于把"双击没反应"留成无头案。
封装轮的定案应当是：**日志与截图也走 `data_dir()`**（`%LOCALAPPDATA%`），
或者明确只支持"绿色目录安装"（装到用户可写路径）。这一条现在**没改**，
因为改了会动 `crash_log` 与截图两处已有口径，属于封装轮的事。

### 2.1 客户端侧现成的四个环境变量口子（都是【实测】）

| 变量 | 读它的地方 | 为什么留 |
| --- | --- | --- |
| `GO_DATA_DIR` | `paths.data_dir()` | 唯一的数据根，后端与客户端一起认（第 4 节） |
| `GO_CLIENT_INI` | `app.main()` → `Prefs(...)` | 验收脚本要注册冒烟账号并写 token，走默认路径会顶掉开发者自己的登录 |
| `GO_CLIENT_LOG_DIR` | `app.main()` → `crash_log.install(...)` | 子进程形态的验收（真两进程跑 `app.py`）没地方传 `install(log_dir=...)`；顺带它也是上面那个只读坑**今天就能用**的绕法，不必等封装轮 |
| `GO_CLIENT_PIPE` | `single_instance.default_name()` | 同机并存两份客户端（两个账号并排比棋）本来就说得通；验收要用它隔开两个真进程 |

【实测】一个连带的坑，写下来免得封装轮重新踩：这台机器的
`.venv\Scripts\python.exe` 是个**重定向器**，它会再投一个真正的解释器进程，
所以 `Popen.pid` 与那个进程里的 `os.getpid()` 不是同一个数（实测 18028 vs 11324）。
任何拿 PID 认子进程的脚本（启动器、封装后的守护、杀子进程）都要先想清楚
拿的是哪一个；`terminate()` 只杀得到壳子，要杀子树得用 `taskkill /F /T /PID`。


## 3. 必须外置、不许进 exe 的东西

| 东西 | 大小 | 为什么外置 |
| --- | --- | --- |
| `backend/katago/katago.exe` + 随包 DLL | 约 22 MB（opencl 版实测 13 个 DLL） | 它是**子进程**，不是 Python 模块；进 onefile 就是每次启动往 temp 解一遍 |
| `backend/katago/models/*.bin.gz` | 实测 94.5 MB | 同上，而且缺一个 DLL 就永远静默降级到启发式 |
| `backend/katago/analysis.cfg` | 1 KB | 【实测】`ensure_config()` 缺必需键时会**重写**它并留 `.bak` —— 只读目录里这一步会抛 |

外置之后要用环境变量把后端指过去（`app/config.py` 的 env 前缀是 `GO_`）：

```
GO_KATAGO_BIN=<安装目录>\katago\katago.exe
GO_KATAGO_MODEL=<安装目录>\katago\models\kata_b18c384nbt-humanv0.bin.gz
GO_KATAGO_CONFIG=<可写目录>\katago\analysis.cfg
```

【实测】三个细节：
1. 文件名不必写死对：`_resolve_model()` 会在"配置里那个文件的**同级目录**"里按
   `b18c384nbt > b18 > b15 > b11 > b10 > b6` 挑实际存在的 `.bin.gz`；
2. 但那个**目录必须存在** —— `path.parent` 不是目录时它直接返回原配置值，症状是
   "装了引擎却永远降级"；
3. 主网络是 human SL 那一类时，`analysis.cfg` 必须有 `humanSLProfile`，
   否则第一条查询就 `FATAL ERROR: SGFMetadata is required`，引擎直接退出。

## 4. 数据搬迁：`GO_DATA_DIR`

后端所有可写数据（SQLite、SGF、复盘报告、KataGo stderr、等级分标定 json）都从
`app.config.DATA_DIR` 派生，而它只认 `GO_DATA_DIR`。【实测】桌面端启动时
`apply_env()` 一定设了它，所以：

- 开发期 = `backend/data`（后端独立跑时与桌面端共用同一份 —— 例如 `start_backend.ps1`
  起一个服务、再开客户端，两边看到的是同一批棋谱）；
- 打包后 = `%LOCALAPPDATA%\Yidao\data`。

**升级不许丢数据**这一条靠的就是它：exe 整个换掉，`%LOCALAPPDATA%` 不动。
卸载时要不要连它一起删，是封装轮要做的决定（默认建议：留着，并在卸载页写清楚）。

## 5. 图标：本轮已完成并【实测】

```
python desktop/scripts/gen_icons.py        # → desktop/resources/app.ico
```

- 画法只有一份：`ui/app_icon.py`（画出来的，仓库里没有 png）；脚本只是把它
  按 7 档尺寸塞进 `.ico` 容器；
- 档位 16/24/32/48/64/128/256 取自 `app_icon.SIZES`，与运行时 `icon()` 同一份清单；
- 容器是自己用 `struct` 拼的（PNG 负载 + 32bpp），因为实测
  `QPixmap.save(x, "ICO")` 一次**只写得下一个尺寸**，而 Windows 是按场合挑尺寸的；
- 【实测】产物能被三个独立口径读通：脚本自带的解析器、Qt 的 ico 插件
  （`QImageReader`，7 页全解出）、以及 Windows 的 GDI+
  （`System.Drawing.Icon` 打开整个文件挑中 32 档；只含 256 的样本报 256x256
  → PNG 负载这条路系统认）；
- 【实测】重跑逐字节一样（`tests/test_icons.py` 钉住），并且**校验发生在写盘之前**：
  把画法改坏（图标全透明）时 `gen_icons.py` 自己非 0 退出，不会写出一个看不见的图标。

封装时：`--icon=desktop/resources/app.ico`，并且要
`--add-data desktop/resources;resources`（13 个 wav 与这张 ico 都在这下面）。

## 6. 已经修掉的一个封装前故障

**题库源码指纹在打包后会抛** ——
`app/tsumego/store.py::_fingerprint()` 原来直接 `read_bytes()` 同目录的
`solve.py / library.py / puzzles.py / store.py`，而 PyInstaller 的包里**没有 .py 源码**。
它在 `library_problems()` 的第一行，抛出去就是"死活练习页永远转圈"。
本轮改成：读不到源码就退回该模块的字节码（`marshal.dumps(loader.get_code(...))`），
再退一步用名字兜底。【实测】

- 新增 `tests/test_tsumego.py::test_fingerprint_survives_a_frozen_install_without_sources`：
  模拟"源码不在" —— 不抛、同进程内稳定、并且**不命中**旧缓存（宁可重算两分钟也别崩）；
- 新增 `test_dev_fingerprint_is_exactly_the_source_hash`：钉住**开发期指纹逐字节不变**，
  所以这条兼容分支不会让别人拉下代码就白等一次重算。
  不过 `store.py` 自己也是指纹源，本轮改了它，因此仍重算并重新提交了
  `library_cache.json`（一次两分钟的 force=True）。

**登记但没修**：`library_cache.json` 是"跟代码放一起的构建产物"，打包后落在 temp 里
→ 每次启动都重算两分钟。封装轮的定案建议：只读种子从 `_BUNDLE` 读，
可写副本放 `data_dir()`。

## 7. 将来封装要写的 spec（**整段都是【推断】，一条没跑过**）

入口名要先处理：`desktop/app.py` 与后端的顶层包 `app/` 同名。开发期以脚本方式跑
没事（入口模块叫 `__main__`），但 PyInstaller 分析时把入口脚本注册成模块 `app`，
和 `--paths backend` 找到的 `app/` 包**撞名**；运行期这个坑已经实测过一次
（`app.py` 顶部 docstring 记着：后端线程里 `'app' is not a package`）。
建议：入口改名 `desktop/main.py`，再封。

```
pyinstaller --noconfirm --clean --name Yidao --windowed `
  --icon desktop/resources/app.ico `
  --add-data "desktop/resources;resources" `
  --paths backend --collect-submodules app `
  --collect-submodules uvicorn `
  --hidden-import anyio._backends._asyncio `
  --hidden-import uvicorn.logging `
  --hidden-import "uvicorn.loops.asyncio" `
  --hidden-import "uvicorn.protocols.http.httptools_impl" `
  --hidden-import "uvicorn.protocols.websockets.websockets_impl" `
  --hidden-import "uvicorn.lifespan.on" `
  desktop/main.py
```

- `--collect-submodules app`：uvicorn 是按字符串 `"app.main:app"` 动态导入的，
  静态分析看不见；
- `--collect-submodules uvicorn` 加那几条 `--hidden-import`：同理，uvicorn 的
  loop / protocol 都是按名字现取；
- `anyio._backends._asyncio`：anyio 的后端也是动态导入；
- **不要 `--onefile`**：每次启动往 temp 解几十 MB，首启慢且杀软误报率高；
  onedir 目录版更像"一个绿色程序"；
- 不要 `--upx`：PySide6 的 DLL 被 UPX 压过是误报重灾区。

上面之外还要留意的（都是【推断】）：Qt 平台插件 `platforms/qwindows.dll`、
多媒体后端（`QSoundEffect` 那条路要的 ffmpeg 组件）、`sqlalchemy` 的 `greenlet`、
`httpx` 要 `certifi` 的 ca 数据。PyInstaller 官方 hook 覆盖其中大部分，
但**以真产物为准，不以文档为准**。

运行期用到的 Qt 模块清单【实测】（`grep` 出来的，不含只在测试里出现的 QtTest）：
`QtCore / QtGui / QtWidgets / QtNetwork / QtMultimedia / QtWebSockets / QtCharts`。

字体【实测】：产品只写 `font-family: "Microsoft YaHei", "Segoe UI", ...`
（`ui/theme.py`），不带字体文件。测试环境里必须额外
`QFontDatabase.addApplicationFont("C:\Windows\Fonts\msyh.ttc")`，因为 offscreen
平台拿不到系统字体 —— 那是**测试**的坑，不是打包的坑，别把补丁搬进产品代码。

## 8. 封装轮必须验收的清单（现在一项都做不了）

1. 真 exe 双击能开，任务栏 / 资源管理器 / alt-tab 三处图标都对；
2. 装到 `C:\Program Files\` 与装到 `D:\Yidao\` 各跑一次 —— 第 2 节那条只读日志
   的坑只有在前者才暴露；
3. 断网首启（LLM 不可达 → 复盘落到模板降级，不许转圈）；
4. 已装 KataGo 的机器上换 exe 版本后引擎仍然可用（第 3 节三个 env 指对了没有）；
5. 升级后旧数据还在（`%LOCALAPPDATA%\Yidao\data` 里的库与 SGF）；
6. 二次启动只唤起已有窗口 —— 本轮【实测】的是源码运行版：
   `test_global_flow.py` 用真子进程 + 真命名管道走通，而且把**两份完整的
   `app.py`** 也各跑了一遍（`test_two_real_client_processes_leave_one_window`：
   第一份的日志里要有「主窗口已出现」，第二份要自己退且不许开窗）；
   启动器侧的单实例锁（`launcher/gui.py::build_gate`）也实测过
   「已在跑就只叫醒、不把短命进程的 PID 记进 `launcher.json`」——
   注：`backend/data/launcher.json` 这个产物已经随旧启动器一起删掉了，
   现在是纯内存 Gate，不再落盘；
7. 退出后没有孤儿 `katago.exe`（优雅关停走 `host.stop()` → lifespan，本轮【实测】
   在源码运行下成立）；
8. 杀软与 SmartScreen：新 exe 无签名时的首启体验（要写进安装说明还是自签，
   是个决定，不是个 bug）；
9. `logs/client.log` 与 `data/logs/katago.stderr.log` 在真 exe 下都真的在写
   （第 2 节的只读坑就靠这条兜底）。
