"""页面共用的几个小积木：卡片、提示条、横向行。

为什么不各写一份：网页版用 CSS class（`.panel` / `.alert.ok` / `.row`）达到同一效果，
原生端如果没有共享组件，每个页面都会 copy 一遍 setStyleSheet，
QSS 一改就得追七个文件 —— 正是本项目吃过的那类"漂移"亏。
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from ui import theme


def hbox(spacing: int = 8) -> QHBoxLayout:
    """一行控件。默认 8px 间距（网页版 .row 的 gap），边距交给外层布局管。"""
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    lay.setContentsMargins(0, 0, 0, 0)
    return lay


class Alert(QFrame):
    """一条提示条（网页版的 `.alert` / `.alert.err`）。文本为空就整体隐藏。"""

    def __init__(self, kind: str = "info", action: str = "", parent=None):
        super().__init__(parent)
        self._kind = ""
        h = hbox(8)
        h.setContentsMargins(10, 7, 10, 7)
        self.label = QLabel("", self)
        self.label.setWordWrap(True)
        # 样式是从父条级联下来的，标签得自己把背景与边框摘掉，否则文字会顶着一个框
        self.label.setStyleSheet("background: transparent; border: none;")
        h.addWidget(self.label, 1)
        self.button: QPushButton | None = None
        if action:
            self.button = QPushButton(action, self)
            self.button.setProperty("role", "ghost")
            self.button.setCursor(Qt.PointingHandCursor)
            self.button.setStyleSheet("background: transparent; border: none; padding: 0 4px;")
            h.addWidget(self.button)
        self.setLayout(h)
        self.set_kind(kind)
        self.setVisible(False)

    @property
    def kind(self) -> str:
        """当前色位。测试与设置页要拿它断言“作废用了警告色而不是绿底”。"""
        return self._kind

    def set_kind(self, kind: str) -> None:
        if kind != self._kind:
            self._kind = kind
            self.setStyleSheet(theme.alert_style(kind))

    def show_text(self, text: str, kind: str | None = None) -> None:
        """显示一行提示；空串等于收起整条。"""
        if kind is not None:
            self.set_kind(kind)
        self.label.setText(text or "")
        self.setVisible(bool(text))


class ElidedLabel(QLabel):
    """单行、装不下就在末尾加省略号、全文进 toolTip 的标签。

    QLabel 自己不会省略：`setWordWrap(True)` 靠换行装长文，会把卡片撑高
    （死活题的「出处」是题库级的一段版权说明，换行后占 5 行，把「本题」卡挤得
    看不见判定框）；`setWordWrap(False)` 则是两头静默裁字（对局页那个
    「手（pass」就是这么来的）。截断这件事必须有人显式做。

    显式做的代价是 `text()` 返回的是**带省略号的显示串**，所以全文另存一份，
    取值走 `fullText()` —— 测试要断言语义就用它，别拿显示串当原文比。
    """

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self._full = text or ""
        self.setWordWrap(False)
        self.setToolTip(self._full)

    def fullText(self) -> str:                                # noqa: N802
        return self._full

    def minimumSizeHint(self) -> QSize:                       # noqa: N802
        """可以缩到很窄 —— 这控件存在的理由就是「装不下就省略」。

        沿用 QLabel 的默认（不分行的标签：最小值 = 原文宽度）会让它反过来
        把整页撑破：设置页四行引擎路径每个 360px，两列一撞就横向溢出（原生没有
        CSS 的 `min-width: 0` 可以拆）。把最小值拍扁之后，空间不够时它自己加省略号，
        而不是让布局去裁别的控件。`sizeHint()` 照旧是全文宽：该要多少还是得说。
        """
        s = super().minimumSizeHint()
        return QSize(min(s.width(), 48), s.height())

    def setText(self, text: str) -> None:                     # noqa: N802
        self._full = text or ""
        self.setToolTip(self._full)
        self._repaint_text()

    def resizeEvent(self, ev):                                # noqa: N802
        super().resizeEvent(ev)
        self._repaint_text()

    def _repaint_text(self) -> None:
        w = self.width() - 2 * self.margin()
        # 还没被布局摆进位置时宽度是 0，这时按原文画（随后一定有 resizeEvent 补一刀）
        QLabel.setText(self, self._full if w <= 0 or self._full == ""
                       else QFontMetrics(self.font()).elidedText(self._full, Qt.ElideRight, w))


class WrapLabel(QLabel):
    """会折行的说明文字。

    **为什么不用裸 `QLabel(wordWrap=True)`**：Qt 对折行标签的 `minimumSizeHint()`
    只按一行算，于是布局在纵向吃紧时能把它压到一行以下 —— 第二行就画到第一行上面，
    用户看到的是「字符重叠」（2026-09-08 用户报的现场，实测 h=25 而 `heightForWidth` 要 34）。
    这里把最小高度改成「按宽度算出来的真实折行高度」，布局就压不扁它：
    空间不够时宁可让页面出滚动条/让别的东西让位，也不许把字画叠。

    `maximumWidth` 一并在构造里定死（调用方给预算），这样 `heightForWidth(最大宽度)`
    就是这块文字永远够用的高度。
    """

    def __init__(self, text: str = "", parent: QWidget | None = None,
                 max_width: int = 470):
        super().__init__(text, parent)
        self.setWordWrap(True)
        if max_width > 0:
            self.setMaximumWidth(max_width)
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        policy.setVerticalPolicy(QSizePolicy.Policy.Minimum)
        self.setSizePolicy(policy)

    def _wrapped_height(self, width: int) -> int:
        h = self.heightForWidth(max(1, width))
        return h if h > 0 else super().sizeHint().height()

    def _reference_width(self) -> int:
        """算最小高度用的参考宽度：**当前宽度**优先。

        不能用 `maximumWidth()`：不封顶的标签（`max_width=0`）那样会算出「一行高」，
        又变回可以被压扁的 QLabel。布局会反复问 minimumSizeHint，宽度收敛后自然就对了。
        """
        if self.width() > 0:
            return self.width()
        cap = self.maximumWidth()
        return cap if cap < 100000 else 470

    def sizeHint(self) -> QSize:                              # noqa: N802
        base = super().sizeHint()
        return QSize(base.width(), self._wrapped_height(self._reference_width()))

    def minimumSizeHint(self) -> QSize:                       # noqa: N802
        base = super().minimumSizeHint()
        return QSize(min(base.width(), 48), self._wrapped_height(self._reference_width()))


class Panel(QFrame):
    """一张卡片（网页版的 `.panel`）：可选标题 + `body` 内容布局。"""

    def __init__(self, title: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setProperty("card", True)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(14, 12, 14, 14)
        self.body.setSpacing(8)
        self.title = QLabel(title, self)
        self.title.setProperty("role", "h3")
        if title:
            self.body.addWidget(self.title)
