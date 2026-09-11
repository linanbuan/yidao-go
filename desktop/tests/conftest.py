"""桌面端测试夹具。

与 `backend/conftest.py` 的分工：那边测服务端（无界面、引擎关闭），这边测客户端
（有 QApplication、需要真平台插件）。**两边分开跑**，混跑会让 233 项后端测试
背上 Qt 的环境依赖，也会让 Qt 测试被后端的 event_loop 夹具干扰。

数据隔离：session 开始就把 `GO_DATA_DIR` 指到 pytest 的 tmp 目录，
所以桌面测试**不会碰**你 `backend/data` 里真实的棋谱与数据库。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

DESKTOP = Path(__file__).resolve().parent.parent
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

#: 平台插件：没人设过它就等于把验收交给 Qt 自己挑。
# 实测（本机，PySide6 6.11.2）：不设 → `platformName()=="windows"`，测试直接跑在
# 开发者那块真屏上（1707x960 逻辑尺寸 / DPR **1.5**），`popup()` 会真弹一个菜单
# 到桌面上；设了 → `"offscreen"`，一块 800x800 的虚拟屏、DPR 1.0。
# 两个后果都是真的：① 像素断言的预算随手换显示器（`restoreGeometry` 会把窗口
# 夹进可用屏幕，1240x780 在 800 宽的屏上会变 1180x774）；② 跑测试时会抢焦点。
# 这里钉住 offscreen（`setdefault`：开发者要拿真屏复现仍可自己 `set`）。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

#: 动效默认关掉。动效是给眼睛的，验收要的是**终态**：`harness.snap/sample` 采到的
#: 那一帧可能正好是棋子淡入到一半（半透明的子）或最后一手的圈还没收到位，
#: 于是像素断言变成"看运气"。要验动效本身的测试自己 `motion.set_enabled(True)`
#: （`set_enabled` 是进程内覆盖，比 env 优先级高，所以放在这里钉不死它）。
os.environ.setdefault("YIDAO_ANIM", "0")

#: 候选字体文件（按优先级）。第一条就是生产用的那个，量出来的字宽与用户看到的一致。
FONT_CANDIDATES: tuple[str, ...] = (
    r"C:\Windows\Fonts\msyh.ttc",             # Microsoft YaHei == theme.FONT_FAMILY
    r"C:\Windows\Fonts\NotoSansSC-VF.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
)

#: 本进程装上的字体 family；没装上就是空串（`harness.snap` 会因此报错）。
CJK_FONT = ""


def install_cjk_font() -> str:
    """给测试进程装一个**真有字形**的字体，返回它的 family 名。

    硬事实（本机实测：PySide6 6.11.2 + `QT_QPA_PLATFORM=offscreen`）：offscreen 平台
    **不注册 Windows 字体库** —— `QFontDatabase.families()` 是空列表，
    `QFontInfo(f).family()` 是空串。于是截图里汉字与 ASCII 一律画成豆腐框
    （.notdef）：一个 `A` 和一个「围」占的像素一模一样。

    后果不是「图难看」而是**验收是假的**：`H.snap` 那套「逐张读关键帧」根本
    读不到字，能读到的只有版式与配色。所以这里把字体装回来，让图真的能读。
    `addApplicationFont` 走 Qt 自带的字体引擎，不依赖平台插件，装完 families
    立刻从 0 变非 0（实测）。
    """
    from PySide6.QtGui import QFontDatabase

    for path in FONT_CANDIDATES:
        if not Path(path).exists():
            continue
        fid = QFontDatabase.addApplicationFont(path)
        if fid < 0:
            continue
        fams = QFontDatabase.applicationFontFamilies(fid)
        if fams:
            return fams[0]
    return ""


@pytest.fixture(scope="session", autouse=True)
def isolated_env(tmp_path_factory):
    """数据进临时目录；引擎**默认**关掉（快、可复现），显式打开时这一趟就是 KataGo 那一支。

    为什么是 `setdefault` 而不是无条件写 false：计划对 P2 的口径是「KataGo 就绪 /
    未就绪两种都要跑」。做法是同一个文件带 `GO_KATAGO_ENABLED=true` 起一个独立的
    pytest 进程再跑一遍，而不是复制一份测试 —— 复制出来的第二份迟早只会测到
    「复制那天还成立的东西」，两支的行为就此漂移。这里一覆盖，外面那个 env 就白设了。
    """
    data = tmp_path_factory.mktemp("go-desktop-data")
    os.environ["GO_DATA_DIR"] = str(data)
    os.environ.setdefault("GO_KATAGO_ENABLED", "false")
    return data


@pytest.fixture(scope="session")
def qapp(isolated_env):
    """整个 session 共用一个 QApplication（Qt 的硬约束：只能有一个）。

    样式统一在夹具里装好，这样每个测试看到的控件外观与真实客户端一致 ——
    否则截图验收看的是"没有主题的假界面"，等于白看。

    平台插件在本模块顶部钉住（见那里）：`configure_hi_dpi()` 只设了
    缩放因子的取整策略（PassThrough），它**不会**把 DPR 调成 1.0 —— 别指望它。
    """
    from PySide6.QtWidgets import QApplication

    from ui import theme

    global CJK_FONT
    theme.configure_hi_dpi()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")        # Fusion 对 QSS 的响应比 windowsvista 稳定且可预测
    CJK_FONT = install_cjk_font()     # 不装字体就是满屏豆腐块，见那里
    theme.apply_app_font(app)
    app.setStyleSheet(theme.QSS)
    yield app


@pytest.fixture
def board_widget(qapp):
    """建一个棋盘并负责收尾销毁，避免测试之间互相看到对方的子控件。"""
    from ui.widgets.board import GoBoard

    widgets = []

    def make(size=19, **kw):
        w = GoBoard(size=size, **kw)
        w.resize(640, 640)
        widgets.append(w)
        return w
    yield make
    for w in widgets:
        w.close()
        w.deleteLater()
