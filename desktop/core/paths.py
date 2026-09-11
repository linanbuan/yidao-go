"""桌面端所有路径的唯一来源。

为什么单独一个文件：打包成 exe 之后 `__file__` 会落到**只读**的临时解包目录
（PyInstaller onefile 的 `sys._MEIPASS`），而数据必须可写、资源必须能定位。
如果各处自己拼路径，将来封装时要全仓改一遍。所以判断只允许写在这一处。

诚实边界：`FROZEN` 分支（打包后）本轮**未实测**，按计划本轮不做封装；
开发期走的是 `FROZEN == False` 那条，它才是被测试覆盖过的路径。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 冻结判定：onefile/onedir 下由 PyInstaller 注入
FROZEN = bool(getattr(sys, "frozen", False))

# 只读资源的根（打包后是解包目录，开发期是 desktop/）
_BUNDLE = Path(getattr(sys, "_MEIPASS", str(Path(__file__).resolve().parent.parent)))

# 可写区域与安装区域的分界：开发期 desktop/，打包后 exe 所在目录
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
PROJECT_ROOT = APP_DIR if FROZEN else APP_DIR.parent
BACKEND_DIR = PROJECT_ROOT / "backend"

RESOURCES_DIR = _BUNDLE / "resources"
SOUNDS_DIR = RESOURCES_DIR / "sounds"
ARTIFACTS_DIR = APP_DIR / "artifacts"          # 截图等验收证据
LOGS_DIR = APP_DIR / "logs"


def data_dir() -> Path:
    """可写数据目录。优先级：显式环境变量 > 打包后的 LOCALAPPDATA > 开发期 backend/data。

    开发期刻意沿用 `backend/data`：本轮不改数据位置，数据库/SGF/日志与网页版共用同一份，
    这样桌面端能看到你在浏览器里下过的棋。
    """
    env = os.environ.get("GO_DATA_DIR")
    if env:
        return Path(env)
    if FROZEN:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Yidao" / "data"
    return BACKEND_DIR / "data"


def apply_env() -> Path:
    """在 import 后端任何模块之前调用。

    做两件事：① 把数据目录交给后端（`app.config` 在 import 时就读 `GO_DATA_DIR`，
    晚设无效）；② 把 `backend/` 放进 `sys.path`，让 `import app.main` 找得到。
    """
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.environ["GO_DATA_DIR"] = str(d)
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    return d
