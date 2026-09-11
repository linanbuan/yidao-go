"""色板与全局样式。

色值全部**抄自网页版**（`frontend/src/styles.css` 的 `:root` 与 `components/GoBoard.tsx`
的绘制常量），不是我自己挑的 —— 冻结的 React 版就是这套视觉的活文档，
原生版没有理由做成另一个样子。改这里时请对照那两个文件。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QApplication

# ---------------------------------------------------------------- 界面色板（styles.css :root）
BG = "#f6f4ef"
PANEL = "#ffffff"
INK = "#212529"
MUTED = "#868e96"
#: 半透明的「归属信息」色（设置页右上角的登录名）。用带 alpha 的字色而不是
#: `QGraphicsOpacityEffect`：效果器会让父控件走离屏渲染、可能留残影。
USER_TAG = "rgba(134, 142, 150, 55%)"
LINE = "#e9ecef"
ACCENT = "#1971c2"
DANGER = "#e03131"
OK = "#2f9e44"
WARN = "#f59f00"

# ---------------------------------------------------------------- 棋盘色（GoBoard.tsx）
BOARD_TOP = "#e8c391"        # 木纹渐变上端
BOARD_BOTTOM = "#d9ab6c"     # 木纹渐变下端
GRID = "#5a4226"
STAR = "#4a3517"
LABEL = "#6b4f2a"
STONE_BLACK_IN = "#5a5a5a"
STONE_BLACK_OUT = "#0b0b0b"
STONE_WHITE_IN = "#ffffff"
STONE_WHITE_OUT = "#d4d4d4"
DEAD_MARK = "#e03131"
LAST_MOVE_ON_BLACK = "#ffffff"
LAST_MOVE_ON_WHITE = "#111111"
HINT_FIRST = "#1971c2"
HINT_REST = "#495057"

# 标记类型 → 颜色（GoBoard.tsx 的 MARK_COLOR）
MARK_COLOR = {
    "blunder": "#e03131",
    "bad": "#f76707",
    "slow": "#f59f00",
    "good": "#2f9e44",
    # 死活题的目标子与自己要救的那块用冷色：不能与「错手/好手」的红绿撞色，
    # 否则题目一摆出来就像已经给了评价
    "target": "#1c7ed6",
    "own": "#7048e8",
}

# 复盘评级 → 颜色（types.ts 的 FLAG_COLOR，与上面 MARK_COLOR 口径不同，别混用）
FLAG_COLOR = {
    "good": "#2f9e44",
    "slow": "#f08c00",
    "bad": "#e8590c",
    "blunder": "#c92a2a",
    "pass": "#868e96",
}

#: 复盘评级**徽章**（styles.css 的 `.badge.flag-good` ~ `.flag-pass` 五行逐条抄来）。
#: 与上面的 FLAG_COLOR 是两套，不是重复：那一套是散点/文字色（要压得住白底，对比强），
#: 这一套是「淡底深字」的徽章底色。合成一套的话，同一个「大恶手」在曲线上是个红点、
#: 在卡片上是个红底块，看着就不像同一件事的两种画法了。
FLAG_BADGE_COLORS = {
    "good": ("#2b8a3e", "#ebfbee"),
    "slow": ("#846a06", "#fff9db"),
    "bad": ("#d9480f", "#fff4e6"),
    "blunder": ("#c92a2a", "#ffe3e3"),
    "pass": ("#868e96", "#f1f3f5"),
}

# 徽章语义（网页版 .badge / .badge.ok / .badge.promo 的等价物）
BADGE_COLORS = {
    "": (ACCENT, "#e7f1fb"),
    "ok": (OK, "#e6f4ea"),
    "warn": ("#b08968", "#fbf3e2"),
    "promo": ("#7048e8", "#efe9fd"),
    "err": (DANGER, "#fdecec"),
    "muted": (MUTED, "#f1f3f5"),
    # 段位徽章：styles.css 的 `.badge.rank` 是自定的淡黄一套（与 .badge.promo 不同），
    # 抄过来而不是复用 promo —— 段位与“晋升战”在一行里会同时出现，撞色就分不开了。
    "rank": ("#664d03", "#fff9db"),
}

# 提示条（styles.css 的 .alert.err/.ok/.info/.warn）：底色 / 边色 / 文字
ALERT_COLORS = {
    "err": ("#fff5f5", "#ffc9c9", "#c92a2a"),
    "ok": ("#ebfbee", "#8ce99a", "#2b8a3e"),
    "info": ("#e7f5ff", "#a5d8ff", "#1864ab"),
    "warn": ("#fff9db", "#ffe066", "#846a06"),
}

FONT_FAMILY = "Microsoft YaHei"

QSS = f"""
QWidget {{
    background: {BG};
    color: {INK};
    font-family: "{FONT_FAMILY}", "Segoe UI", system-ui, sans-serif;
    font-size: 14px;
}}
QMainWindow, QDialog {{ background: {BG}; }}

/* 标签一律透明。上面那条 `QWidget {{ background: BG }}` 会连 QLabel 一起染上底色，
 * 于是白卡片里的普通标签（如登录页的「账号」「密码」）会顶着一块灰底 —— 看截图才发现。
 * 标签本来就是画在父控件背景上的东西，透明才是对的行为。放在 role 规则之前，
 * 下面的 `QLabel[role="badge"]` 靠属性选择器的更高优先级照旧有自己的底色。*/
QLabel {{ background: transparent; }}

/* 卡片：网页版的 .panel 是白底 + 1px 浅边 + 8px 圆角 */
QFrame[card="true"] {{
    background: {PANEL};
    border: 1px solid {LINE};
    border-radius: 8px;
}}
QLabel[role="title"] {{ font-size: 22px; font-weight: 700; background: transparent; }}
QLabel[role="h2"] {{ font-size: 18px; font-weight: 600; background: transparent; }}
QLabel[role="h3"] {{ font-size: 15px; font-weight: 600; background: transparent; }}
QLabel[role="muted"] {{ color: {MUTED}; font-size: 13px; background: transparent; }}
QLabel[role="stat"] {{ font-size: 20px; font-weight: 700; background: transparent; }}
QLabel[role="badge"] {{
    border-radius: 9px; padding: 1px 8px; font-size: 12px; font-weight: 600;
    background: #e7f1fb; color: {ACCENT};
}}

QPushButton {{
    background: {PANEL};
    border: 1px solid #d3dae3;
    border-radius: 6px;
    padding: 6px 14px;
}}
QPushButton:hover {{ background: #f1f3f5; }}
QPushButton:pressed {{ background: #e9ecef; }}
QPushButton:disabled {{ color: {MUTED}; background: #f1f3f5; }}
QPushButton[role="primary"] {{
    background: {ACCENT}; border: 1px solid {ACCENT}; color: white; font-weight: 600;
}}
QPushButton[role="primary"]:hover {{ background: #1864ab; }}
QPushButton[role="primary"]:disabled {{ background: #a5d8ff; color: #f8f9fa; }}
QPushButton[role="danger"] {{ color: {DANGER}; border-color: #ffc9c9; }}
QPushButton[role="ghost"] {{ border: none; background: transparent; color: {ACCENT}; }}
QPushButton[role="ghost"]:hover {{ background: #f1f3f5; }}

QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit {{
    background: {PANEL};
    border: 1px solid #d3dae3;
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {{
    border: 1px solid {ACCENT};
}}

/* 导航：选中项换成浅底 + 主色文字，对应 .nav a.active */
QToolButton {{
    background: transparent; border: none; padding: 6px 12px;
    border-radius: 6px; font-size: 14px;
}}
QToolButton:hover {{ background: #f1f3f5; }}
QToolButton:checked {{ background: #e7f1fb; color: {ACCENT}; font-weight: 600; }}

QListWidget, QTableWidget, QTreeView {{
    background: {PANEL}; border: 1px solid {LINE}; border-radius: 8px;
    padding: 4px;
}}
QListWidget::item {{ padding: 8px 10px; border-radius: 6px; }}
QListWidget::item:hover {{ background: #f8f9fa; }}
QListWidget::item:selected {{ background: #e7f1fb; color: {INK}; }}

QTabBar::tab {{
    background: transparent; padding: 7px 16px; border: none; color: {MUTED};
}}
QTabBar::tab:selected {{ color: {ACCENT}; font-weight: 600; border-bottom: 2px solid {ACCENT}; }}

/* 进度条：10px 高的细槽，**槽内不画字**（网页版两条进度条都是纯色槽，阶段文案与
   百分比放在槽外单独一行：大厅是上方的 label、复盘是 `.progbar-text`）。
   这里原先写了一行 `text: none;` —— QSS 没有 `text` 这个属性，Qt 不报错、
   只往 stderr 丢一句 `Unknown property text` 就把它丢掉（实测 5 个测试刷了 29 句，
   止住这种噪音才能看见同批输出里真正要紧的那句
   `QLayout: ... already has a layout`）。网页版 styles.css 里那条「复用 progress
   会把文字裁掉」的注释说的是同一个坑。 */
QProgressBar {{
    background: #ececec; border: none; border-radius: 5px; height: 10px;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}

QScrollArea {{ border: none; background: transparent; }}
QSplitter::handle {{ background: {LINE}; }}

/* 滚动条刻意做细：默认 17px 的 Windows 滚动条在卡片里非常抢眼 */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #ced4da; border-radius: 4px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: #adb5bd; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #ced4da; border-radius: 4px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; background: none; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QToolTip {{
    background: #343a40; color: white; border: none; padding: 5px 8px;
    font-size: 12px; border-radius: 4px;
}}
QMessageBox {{ background: {PANEL}; }}

/* 菜单栏与菜单（P5）。两条口径：
   ① `QMenuBar::item:selected` 用主色淡底（与导航 `QToolButton:checked` 同一套），
     菜单项是「按下去就有一个东西亮起来」的控件，跟 Fusion 默认的深蓝选中条
     放在一起会显得是另一个程序；
   ② 纵向 padding 刻意小（1px/3px）：这条菜单条是**摆在窗口布局里**的，
     它的每一像素都从页面视口里扣（实测 1280x800 下内容区从 771 高变 746 高），
     不像真的原生菜单条那样悬浮在客户区之外。*/
QMenuBar {{
    background: {PANEL}; border-bottom: 1px solid {LINE}; padding: 1px 6px;
}}
QMenuBar::item {{ background: transparent; padding: 3px 10px; border-radius: 4px; }}
QMenuBar::item:selected {{ background: #e7f1fb; color: {ACCENT}; }}
QMenuBar::item:pressed {{ background: #dcebf9; color: {ACCENT}; }}
QMenuBar::item:disabled {{ color: {MUTED}; }}
QMenu {{ background: {PANEL}; border: 1px solid {LINE}; padding: 4px; }}
QMenu::item {{ background: transparent; padding: 5px 22px 5px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: #e7f1fb; color: {ACCENT}; }}
QMenu::item:disabled {{ color: {MUTED}; }}
QMenu::separator {{ height: 1px; background: {LINE}; margin: 4px 6px; }}
"""


def configure_hi_dpi() -> None:
    """必须在**创建 QApplication 之前**调用。

    PassThrough 而不是 Round：本机 DPR 是 1.5，Round 会把它取整成 2 或 1，
    于是同一套像素预算在不同屏幕上量出来不一样，棋盘格线也会出现
    spike 里那种深浅不一（半像素线被取整后有的落格心、有的跨格心）。
    """
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )


def apply_app_font(app: QApplication) -> None:
    font = QFont(FONT_FAMILY, 10)
    font.setHintingPreference(QFont.PreferFullHinting)
    app.setFont(font)


def color(name: str) -> QColor:
    return QColor(name)


def _pill(fg: str, bg: str) -> str:
    """一枚胶囊。形状与字号只有一份，`badge_style` 与 `flag_badge_style` 只差配色。"""
    return (f"color: {fg}; background: {bg}; border-radius: 9px;"
            f" padding: 1px 8px; font-size: 12px; font-weight: 600;")


def badge_style(kind: str) -> str:
    fg, bg = BADGE_COLORS.get(kind, BADGE_COLORS[""])
    return _pill(fg, bg)


def flag_badge_style(flag: str) -> str:
    """按复盘评级上色的徽章。认不出的评级落到「虚手」那套灰，而不是落到蓝：
    蓝是本应用的主色（按钮、选中态），一个没认出的评级顶着主色会被读成可点的东西。"""
    fg, bg = FLAG_BADGE_COLORS.get(flag, FLAG_BADGE_COLORS["pass"])
    return _pill(fg, bg)


def alert_style(kind: str) -> str:
    """提示条的 QSS。`background: transparent` 那条 QLabel 规则会把它顶掉，
    所以这里必须显式给底色（不写 border 则 Fusion 会把 QLabel 画成凸起的一块）。"""
    bg, border, fg = ALERT_COLORS.get(kind, ALERT_COLORS["info"])
    return (f"color: {fg}; background: {bg}; border: 1px solid {border};"
            f" border-radius: 8px; padding: 8px 10px; font-size: 13px;")
