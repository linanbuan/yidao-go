"""整局总评的排版件。

用户原话：「以这部分为例，文字相当之挤，阅读起来很累」。所以这一块的排版口径是：

  · **一句一行**：`overall` 按中文句末标点切开，每句一个标签。一整段墙再短，也比
    「四五个短句各占一行」难读得多 —— 阅读负担主要来自「在一行里找下一句的起点」。
  · **最该看见的那句抬成 callout**：含「胜负分界」的那句单独摆一条左侧主色竖条的提示块。
    模板一定会写这句；大模型不写就没有 callout，不会报错也不会留个空框。
  · **数字上卡、流水账下台**：布局/中盘/官子用后端已经给的结构化 `phases`
    摆成三张小卡（手数 / 均损 / 最大损失），模板那句「布局阶段共 19 手，平均每手
    损失 2.6 目。」不再原样贴出来（数字已经在卡里）。拿不到结构数据（大模型散文）时
    退回原文 —— **不硬拆散文**。
  · **训练建议拆两行**：`练「战场方向」：落子前…` → 标题一行、正文一行，缩进对齐。
  · 行距靠**块间距**，不靠 CSS `line-height`：QLabel 纯文本不支持它，而富文本会让
    `text()` 变成 HTML —— 测试与「取原文」都会失真（这个项目的断言大量读 `text()`）。

这个类只做排版，**不做取数与格式化**：`review.py` 把数字先算好、文案先洗好，按下面的
字典喂进来。这样它的单元测试不需要造一整套复盘报告。
"""
from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QLabel, QVBoxLayout, QWidget,
)

from ui import theme
from ui.widgets.parts import Panel, WrapLabel, hbox

#: 句末标点。中文分号也断：`…方向偏了；AI 为 6.9 目。` 断成两行更好读。
SENT_END = "。！？；!?;"
#: 模板那句流水账的开头，用于把它从「阶段评语」里剥掉（数字已经在卡里了）。
_TEMPLATE_HEAD = re.compile(
    r"^[^。！？；]{0,24}?阶段共\s*\d+\s*手[，,]平均每手损失\s*[\d.]+\s*目[。.]?")
#: 「标题：正文」的第一次分隔。
_LEAD_SEP = re.compile(r"^([^：:]{1,18})[：:]\s*(.*)$", re.S)


def split_sentences(text: str) -> list[str]:
    """按句末标点（含换行）切句，标点跟着前一句，空段丢掉。"""
    out: list[str] = []
    buf = ""
    for ch in (text or "").strip():
        if ch in "\n\r":
            if buf.strip():
                out.append(buf.strip())
            buf = ""
            continue
        buf += ch
        if ch in SENT_END:
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def split_training(text: str) -> tuple[str, str]:
    """`练「战场方向」：落子前先问…` → `("练「战场方向」", "落子前先问…")`。

    没有分隔符（或分隔符出现得太晚，像正文里的冒号）时整句当正文，标题留空。
    """
    m = _LEAD_SEP.match((text or "").strip())
    if not m:
        return "", (text or "").strip()
    return m.group(1).strip(), m.group(2).strip()


def phase_tone(text: str) -> str:
    """阶段评语：剥掉模板那句数字流水账，留后面的评语。

    只有**认得出模板句式**时才剥（大模型写的散文原样返回）—— 认错就会把评语吃掉，
    那是比多一行数字严重得多的错。
    """
    t = (text or "").strip()
    if not t:
        return ""
    stripped = _TEMPLATE_HEAD.sub("", t, count=1).strip()
    return stripped or t


def first_clause(text: str) -> str:
    """取第一小节（到第一个逗号/句号为止），用来压成一行评语。"""
    t = (text or "").strip()
    for i, ch in enumerate(t):
        if ch in "，,。！？；;":
            return t[:i].strip()
    return t


def pivot_sentence(sentences: list[str]) -> tuple[str, list[str]]:
    """把「胜负分界」那句从正文里摘出来，返回 `(那句, 其余句子)`。"""
    pivot = ""
    rest: list[str] = []
    for s in sentences:
        if not pivot and "胜负分界" in s:
            pivot = s
        else:
            rest.append(s)
    return pivot, rest


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())


class SummaryPanel(Panel):
    """「整局总评」卡片。`render(data)` 重建全部内容。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__("", parent)          # 标题自己画：要和结果徽章同一行
        self.body.setSpacing(8)

    # ---------------------------------------------------------------- 入口
    def render(self, data: dict) -> None:
        _clear(self.body)
        self._build_head(data)
        if data.get("gauges"):
            self.body.addWidget(self._gauge(data["gauges"]))
        if data.get("counts"):
            self.body.addWidget(self._counts(data["counts"]))
        if data.get("sentences"):
            self.body.addWidget(self._prose(data["sentences"]))
        if (data.get("pivot") or "").strip():
            self.body.addWidget(self._callout("胜负分界", str(data["pivot"]).strip()))
        if data.get("phases"):
            self.body.addWidget(self._phase_rows(data["phases"]))
            if (data.get("phase_notes") or "").strip():
                note = QLabel(str(data["phase_notes"]), self)
                note.setProperty("role", "muted")
                note.setWordWrap(True)
                self.body.addWidget(note)
        else:
            for label, text in (data.get("phase_fallback") or []):
                self.body.addWidget(self._kv(label, text))
        if data.get("training"):
            box = QWidget(self)
            col = QVBoxLayout(box)
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(4)                       # 条目之间比块之间更紧
            head = QLabel("训练建议", box)
            head.setProperty("role", "h3")
            col.addWidget(head)
            for i, item in enumerate(data["training"], start=1):
                lead, body = (item if isinstance(item, (tuple, list)) else ("", item))
                col.addWidget(self._training(i, lead, body))
            self.body.addWidget(box)
        if (data.get("maxim") or "").strip():
            self.body.addWidget(self._maxim(f"「{str(data['maxim']).strip()}」"))
        if (data.get("foot") or "").strip():
            foot = QLabel(str(data["foot"]), self)
            foot.setProperty("role", "muted")
            foot.setWordWrap(True)
            self.body.addWidget(foot)

    # ---------------------------------------------------------------- 各块
    def _build_head(self, data: dict) -> None:
        row = hbox(8)
        title = QLabel(str(data.get("title") or ""), self)
        title.setProperty("role", "h3")
        row.addWidget(title)
        row.addStretch(1)
        result = str(data.get("result") or "").strip()
        if result:
            row.addWidget(self._badge(f"结果 {result}", kind="muted"))
        self.body.addLayout(row)

    def _gauge(self, gauges) -> QWidget:
        """三格数字。值和标题都由调用方格式化好（`0.84` 这种精度口径在 review.py）。"""
        box = QWidget(self)
        grid = QGridLayout(box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        for i, (value, caption) in enumerate(gauges):
            cell = QFrame(box)
            cell.setProperty("card", True)
            cell.setStyleSheet("background: #f8f9fa; border: 1px solid "
                               f"{theme.LINE}; border-radius: 10px;")
            v = QVBoxLayout(cell)
            v.setContentsMargins(8, 7, 8, 7)
            v.setSpacing(1)
            big = QLabel(str(value), cell)
            big.setProperty("role", "stat")
            big.setAlignment(Qt.AlignCenter)
            small = QLabel(str(caption), cell)
            small.setProperty("role", "muted")
            small.setAlignment(Qt.AlignCenter)
            small.setWordWrap(True)
            if "吻合度" in str(caption):
                small.setToolTip("平均每手损失目数（越小越好）")
            v.addWidget(big)
            v.addWidget(small)
            grid.addWidget(cell, 0, i)
        return box

    def _counts(self, counts) -> QWidget:
        """一行计数徽章（结果徽章在标题行，所以这里只有四个，一行放得下）。"""
        box = QWidget(self)
        row = hbox(6)
        row.setContentsMargins(0, 0, 0, 0)
        for label, value, flag in counts:
            row.addWidget(self._badge(f"{label} {value}", flag=flag))
        row.addStretch(1)
        box.setLayout(row)
        return box

    def _prose(self, sentences) -> QWidget:
        box = QWidget(self)
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(3)
        for sentence in sentences:
            v.addWidget(WrapLabel(str(sentence), box, max_width=0))
        return box

    def _callout(self, title: str, text: str) -> QWidget:
        box = QFrame(self)
        box.setObjectName("summaryCallout")
        box.setStyleSheet(
            f"QFrame#summaryCallout {{ background: #f1f8ff; border: none;"
            f" border-left: 3px solid {theme.ACCENT}; border-radius: 6px; }}")
        row = hbox(8)
        row.setContentsMargins(10, 7, 10, 7)
        head = QLabel(title, box)
        head.setStyleSheet(f"color: {theme.ACCENT}; font-weight: 600; background: transparent;")
        row.addWidget(head, 0, Qt.AlignTop)
        row.addWidget(WrapLabel(text, box, max_width=0), 1)
        box.setLayout(row)
        return box

    def _phase_rows(self, phases) -> QWidget:
        """三阶段一行一段，装在同一个浅底卡片里。

        为什么不是三张小卡并排：卡宽只有面板的 1/3（最小 330px 时一张才 ~100px），
        而「最大 24.0 目 · 第 74 手」要 140px —— 挤不下时 Qt 只把字裁掉（首跑就被
        `clipped_texts` 抓住：宽 47 < 需要 144）。一行一段则把宽度让给最长的那列。
        """
        box = QFrame(self)
        box.setStyleSheet("background: #f8f9fa; border: 1px solid "
                          f"{theme.LINE}; border-radius: 10px;")
        grid = QGridLayout(box)
        grid.setContentsMargins(12, 8, 12, 8)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(3, 1)
        for i, p in enumerate(phases):
            name = QLabel(str(p.get("label") or ""), box)
            name.setStyleSheet("font-weight: 600; background: transparent;")
            moves = QLabel(f"{p.get('moves', 0)} 手", box)
            moves.setProperty("role", "muted")
            avg = QLabel(f"均损 {p.get('avg')} 目", box)
            avg.setStyleSheet(f"color: {p.get('avgColor') or theme.INK};"
                              " font-weight: 600; background: transparent;")
            worst = WrapLabel(f"最大 {p.get('worst')} 目 · 第 {p.get('worstNum')} 手",
                              box, max_width=0)
            worst.setProperty("role", "muted")
            grid.addWidget(name, i, 0)
            grid.addWidget(moves, i, 1)
            grid.addWidget(avg, i, 2)
            grid.addWidget(worst, i, 3)
        return box

    def _kv(self, caption: str, value: str) -> QWidget:
        """拿不到结构数据时的兜底：`布局 | 长文本` 两列（值列会折行）。"""
        box = QWidget(self)
        row = hbox(8)
        row.setContentsMargins(0, 0, 0, 0)
        cap = QLabel(caption, box)
        cap.setProperty("role", "muted")
        cap.setFixedWidth(36)
        cap.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        row.addWidget(cap)
        row.addWidget(WrapLabel(value, box, max_width=0), 1)
        box.setLayout(row)
        return box

    def _training(self, index: int, lead: str, body: str) -> QWidget:
        """一行一条：圆号 + 原文（`练弃取：提子前先问…`）。

        原来把标题和正文拆成两行更好看，但 5 条就多 5 行（实测宽窗口下 +95px）——
        用户要「少滚动」，这里用编号圆点提供结构、把行数省回来。
        """
        box = QWidget(self)
        row = hbox(8)
        row.setContentsMargins(0, 0, 0, 0)
        num = QLabel(str(index), box)
        num.setFixedSize(20, 20)
        num.setAlignment(Qt.AlignCenter)
        num.setStyleSheet(f"background: {theme.ACCENT}; color: #ffffff;"
                          " border-radius: 10px; font-weight: 600;")
        row.addWidget(num, 0, Qt.AlignTop)
        text = f"{lead}：{body}" if lead and body else (lead or body)
        row.addWidget(WrapLabel(text, box, max_width=0), 1)
        box.setLayout(row)
        return box

    def _maxim(self, text: str) -> QLabel:
        lab = QLabel(text, self)
        lab.setStyleSheet("color: #846a06; font-style: italic; background: transparent;"
                          " border: none;")
        lab.setWordWrap(True)
        return lab

    def _badge(self, text: str, flag: str = "", kind: str = "") -> QLabel:
        lab = QLabel(text, self)
        lab.setProperty("role", "badge")
        lab.setStyleSheet(theme.flag_badge_style(flag) if flag else theme.badge_style(kind))
        return lab
