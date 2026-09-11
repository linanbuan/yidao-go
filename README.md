# 弈道 | Yidao

> **中文** | [English](#english)

---

## 中文

从 18 级练到九段的单机 AI 围棋教学：对弈、复盘、死活题、段位晋升，全部离线运行。

- **对弈**：KataGo 引擎（human SL 模型）+ 级位削弱体系，18 级到九段逐档校准
- **复盘**：逐手胜率曲线、AI 吻合度（平均每手损失目数）、LLM 讲评（可选，DeepSeek/OpenAI 兼容接口）
- **死活题**：416 题，18 级～9 段分级；引擎局部搜索出题与判题（DEAD/KO/SEKI/ALIVE 四值结论）
- **段位系统**：晋升战（含吻合度门槛）、等级分、对局日历
- **桌面客户端**：PySide6，双端入口（启动器 exe / 脚本），内嵌后端，免部署

### 架构

```
弈道启动器.exe        ← 双击启动（或 启动围棋平台.vbs / .bat）
launcher/gui.py         启动器：登录 + 启动合一，内嵌后端
desktop/                PySide6 客户端（自带内嵌后端，随机端口）
backend/                FastAPI + WebSocket + SQLite + KataGo 引擎池
  └─ katago/download.py 引擎与权重下载器（GitHub + 镜像加速，cuda→opencl→eigen 自动降级）
```

后端与客户端同进程内嵌（uvicorn 线程），数据落 `backend/data/`（SQLite + SGF + 日志）。

### 快速开始

```powershell
# 1. 环境引导（建 backend/desktop 两个 venv、装依赖、PySide6 瘦身与自检）
powershell -ExecutionPolicy Bypass -File scripts\setup_env.ps1

# 2. 下载 KataGo 引擎与权重（约几百 MB，装一次即可；也可在启动器里点「安装 KataGo」）
cd backend && ..\desktop\.venv\Scripts\python.exe katago\download.py

# 3. 双击 启动围棋平台.vbs（或 弈道启动器.exe，需先按上文命令自行打包）
```

### 开发

```powershell
# 后端测试（239 项）
cd backend  && .venv\Scripts\python.exe -m pytest tests -q
# 桌面端测试（393+ 项；带 GO_KATAGO_ENABLED=true 可加跑真引擎支）
cd desktop  && .venv\Scripts\python.exe -m pytest tests -q
```

### 说明

- 开源协议：**MIT**（见 [LICENSE](LICENSE)）；KataGo 引擎与权重为 Apache-2.0，由脚本按官方发布下载。

- KataGo 引擎与权重**不入库**：克隆后由 `backend/katago/download.py` 自动下载
  （KataGo，Apache-2.0；human SL 权重）。GPU 不是必需——无显卡自动降级 CPU（eigen，较慢）。
- LLM 讲评为可选项：在启动器或客户端设置里填 API Key 即可；Key 经 DPAPI 加密入库。
- 首次启动时题库会现场推导一遍（约两分钟）并写入本地缓存，之后即秒级加载。
- 本项目为初学者的第一版作品，代码结构与设计如有不成熟之处，欢迎通过 Issue 指正。

---

## English

A standalone AI-powered Go (Weiqi/Baduk) teaching platform that takes you from 18-kyu up to 9-dan: playing, review, life-and-death problems, and rank promotion — fully offline.

- **Play**: KataGo engine (human SL model) + a handicapped strength ladder, calibrated for every level from 18-kyu to 9-dan
- **Review**: move-by-move win-rate curves, AI accuracy (average loss points per move), optional LLM commentary (DeepSeek / OpenAI-compatible APIs)
- **Tsumego**: 416 problems, graded from 18-kyu to 9-dan; problems generated and judged by local engine search (DEAD / KO / SEKI / ALIVE verdicts)
- **Rank system**: promotion matches (with accuracy gates), Elo rating, game calendar
- **Desktop client**: PySide6, dual entry points (launcher exe / scripts), embedded backend, zero deployment

### Architecture

```
YidaoLauncher.exe       ← double-click to start (or via .vbs / .bat scripts)
launcher/gui.py          launcher: login + start in one window, embedded backend
desktop/                 PySide6 client (embedded backend, random port)
backend/                 FastAPI + WebSocket + SQLite + KataGo engine pool
  └─ katago/download.py  engine & weights downloader (GitHub + mirrors, cuda→opencl→eigen fallback)
```

The backend runs embedded in the client process (uvicorn thread); data lives in `backend/data/` (SQLite + SGF + logs).

### Quick Start

```powershell
# 1. Environment bootstrap (creates venvs, installs deps, slims PySide6, self-check)
powershell -ExecutionPolicy Bypass -File scripts\setup_env.ps1

# 2. Download KataGo engine & weights (a few hundred MB, one-time; also available via "Install KataGo" in the launcher)
cd backend && ..\desktop\.venv\Scripts\python.exe katago\download.py

# 3. Double-click 启动围棋平台.vbs (or 弈道启动器.exe, which you need to build first — see above)
```

### Development

```powershell
# Backend tests (239 items)
cd backend  && .venv\Scripts\python.exe -m pytest tests -q
# Desktop tests (393+ items; set GO_KATAGO_ENABLED=true to also run real-engine tests)
cd desktop  && .venv\Scripts\python.exe -m pytest tests -q
```

### Notes

- License: **MIT** (see [LICENSE](LICENSE)); the KataGo engine and weights are Apache-2.0, downloaded from official releases by the script.

- The KataGo engine and weights are **not included** in this repo: run `backend/katago/download.py` after cloning
  (KataGo, Apache-2.0; human SL weights). A GPU is not required — without one it falls back to CPU (eigen, slower).
- LLM commentary is optional: fill in an API key in the launcher/client settings; keys are encrypted (DPAPI) before storage.
- On first startup the tsumego library is derived on the fly (about two minutes) and cached locally; later starts are instant.
- This is a first-version project by a beginner — if you spot immaturity in the code or design, issues are welcome.
