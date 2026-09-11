"""段位表：18级 → 九段 全 27 档的晋升条件与 AI 棋力配置（徽章墙）。

对应 frontend/src/pages/RanksPage.tsx。数据只有一个 GET：/api/ranks。

字段的**大小写混用**是后端的既有形状，不是这里写错了：`/api/ranks` 直接吐
`RankInfo.to_dict()`（`asdict` → snake_case：rank_id / ai_name / wins_required …），
只有 `engine` 那一段在 `api/system.py` 里被手工换成了 camelCase
（maxVisits / tolerance / localNoise / humanModel）。所以本文件里两种键名同时出现
是对的 —— 把它们统一成一种得改后端接口，而那个接口已被网页版与测试用着。

排版上比网页版多做一件事：把表格关进自己的可视高度里（只有表内滚），
标题与说明常驻。网页版是整页滚，滚到第 27 行就看不见表头了。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QLabel, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from ui.widgets.parts import Alert, Panel

from .. import theme

#: 表头。列宽在 _build_table 里逐列给，理由同处注释。
COLUMNS: tuple[tuple[str, int], ...] = (
    ("段位", 150),
    ("AI 对手", 168),
    ("触发晋升战", 92),
    ("晋升战要求", 92),
    ("吻合度门槛", 112),
    ("棋力参考", 76),
    ("引擎配置", 0),                     # 0 = 剩余宽度全给它
)

DESC = ("每级都有专属 AI 对手人设与棋力配置。达到本级要求胜场后进入晋升战，"
        "晋升战需连续获胜（5段以上还要吻合度达标）才能升段；教学模式下默认不降级。")

#: 表格下方的四条说明，逐条抄自网页版。
NOTES: tuple[str, ...] = (
    "吻合度 = 平均每手损失目数（引擎逐手比对最佳点得出），数值越小说明棋力越强；"
    "职业棋手通常在 0.5 目以内。",
    "级位～1级使用拟人棋风：AI 会犯该级别典型的错误，而不是一味下\u201c弱但正确\u201d的棋。",
    "段位档位逐步提高推演量；六段以上开启持续思考，接近满状态对弈。",
    "「棋力参考」是各档 AI 对手的强度标定值，仅供参考，不等于段位强弱。",
)


def engine_text(engine: dict | None) -> str:
    """一行引擎配置（玩家向）。

    单位用「目」不用温度：学员看得懂「每手容差 2 目」，看不懂「温度 1.5」——
    这一条口径是网页版先定下的。同理，`visits` / `human SL` / `ponder` 都是
    引擎术语，这里一律换成学员听得懂的说法（第 33 轮清除开发者痕迹）。
    """
    eng = engine or {}
    parts = [f"每手 {eng.get('maxVisits', '—')} 次推演"]
    tolerance = float(eng.get("tolerance") or 0)
    parts.append(f"每手容差 {tolerance:g} 目" if tolerance > 0 else "只下最优点")
    noise = float(eng.get("localNoise") or 0)
    if noise > 0:
        parts.append(f"只看局部 {int(round(noise * 100))}%")
    blunder = float(eng.get("blunderRate") or 0)
    if blunder > 0:
        parts.append(f"失误率 {int(round(blunder * 100))}%")
    if eng.get("humanModel"):
        parts.append("拟人棋风")
    if eng.get("ponder"):
        parts.append("持续思考")
    return "　·　".join(parts)


def acc_text(value) -> str:
    return f"≤ {value:g} 目/手" if value is not None else "—"


class RanksPage(QWidget):
    """段位体系页。"""

    def __init__(self, api, parent=None):
        super().__init__(parent)
        self._api = api
        self._items: list[dict] = []
        self._current_rank = None

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        title = QLabel("段位体系：18级 → 九段", self)
        title.setProperty("role", "title")
        desc = QLabel(DESC, self)
        desc.setProperty("role", "muted")
        desc.setWordWrap(True)
        self.errorBar = Alert("err", "知道了", self)
        self.errorBar.button.clicked.connect(lambda: self.errorBar.show_text(""))
        root.addWidget(title)
        root.addWidget(desc)
        root.addWidget(self.errorBar)

        self.table = self._build_table()
        root.addWidget(self.table, 1)

        panel = Panel("说明", self)
        for note in NOTES:
            line = QLabel("· " + note, panel)
            line.setProperty("role", "muted")
            line.setWordWrap(True)
            panel.body.addWidget(line)
        root.addWidget(panel)

    def _build_table(self) -> QTableWidget:
        t = QTableWidget(0, len(COLUMNS), self)
        t.setHorizontalHeaderLabels([name for name, _w in COLUMNS])
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.NoSelection)
        t.setShowGrid(False)
        header = t.horizontalHeader()
        # 整表列宽全部 Fixed：“段位/AI 对手”两列的文字长短差很多，
        # 交给 ResizeToContents 会按最宽那一行撑爆，最后一列反而没地方放
        # “引擎配置”那串长字。定宽 + stretchLastSection 才能把剩下的宽度给它。
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        header.setStretchLastSection(True)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for i, (_name, width) in enumerate(COLUMNS):
            if width:
                t.setColumnWidth(i, width)
        t.verticalHeader().setVisible(False)
        return t

    # ---------------------------------------------------------------- 数据

    def refresh(self) -> None:
        self._api.get("/api/ranks").finished.connect(self._on_loaded)

    def _on_loaded(self, data, err) -> None:
        if err is not None or not isinstance(data, dict):
            self.errorBar.show_text(f"读取段位表失败：{err}", "err")
            return
        # 拉到数据就把上一次的红条收掉（审计 M7）：错误条只增不减，界面就会
        # 长期挂着一条与内容矛盾的失败提示（重试成功了它也不消失）。
        self.errorBar.show_text("")
        self._items = data.get("items") or []
        self._current_rank = data.get("currentRankId")
        self._fill()

    def _fill(self) -> None:
        t = self.table
        t.clearContents()
        t.setRowCount(len(self._items))
        for row, item in enumerate(self._items):
            rank_id = int(item.get("rank_id") or 0)
            is_current = self._current_rank is not None and rank_id == int(self._current_rank)
            reached = self._current_rank is not None and rank_id < int(self._current_rank)
            cells = [
                self._rank_text(item, is_current, reached),
                f"{item.get('ai_name', '')}\n{item.get('ai_title', '')}",
                f"{item.get('wins_required', '—')} 胜",
                f"{item.get('promo_streak', '—')} 连胜",
                acc_text(item.get("max_avg_loss_points")),
                str(item.get("elo", 0)),
                engine_text(item.get("engine")),
            ]
            for col, text in enumerate(cells):
                cell = QTableWidgetItem(str(text))
                # TextWordWrap 是 Qt::Text_AlignmentFlag 里的对齐位，不是字体设置：
                # QStyledItemDelegate 只在 alignment 带上它时才换行。不开的话
                # 「引擎配置」那一长串会被裁成「…　·　只看局」—— 而表格控件不在
                # harness.clipped_texts 的扫描范围内（它只看 QLabel / QPushButton），
                # 这类截断只能靠这里显式开 + 下面按内容定行高来防。
                cell.setData(Qt.TextAlignmentRole,
                             Qt.AlignLeft | Qt.AlignVCenter | Qt.TextWordWrap)
                if col == 0:
                    font = cell.font()
                    font.setBold(is_current)
                    cell.setFont(font)
                    if is_current:
                        cell.setForeground(QColor(theme.ACCENT))
                if col >= 2 and col <= 5:
                    cell.setData(Qt.TextAlignmentRole,
                                 Qt.AlignRight | Qt.AlignVCenter)
                if is_current:
                    cell.setBackground(QBrush(QColor("#e7f5ff")))
                t.setItem(row, col, cell)
            # 行高交给内容：开了 TextWordWrap 的格子要几行由折行决定。
            # 逐行算而不是 `resizeRowsToContents()`：本行刚设完内容，此时只算这一行最省，
            # 全表重算留给后面的批量操作去做。
            t.resizeRowToContents(row)

    def _rank_text(self, item: dict, is_current: bool, reached: bool) -> str:
        """段位那一列。

        网页版用徽章 + 小字，表格里合成文字：在单元格里放徽章得用 cellWidget
        （27 行 × 一个真控件的创建与布局开销不值当），而信息一个字没少。
        两行是为了与右边「AI 对手」那一列的人设名/描述同一个节奏。
        """
        name = item.get("name") or "—"
        short = item.get("short") or ""
        text = f"{name}　{short}" if short else name
        if is_current:
            return text + "\n当前"
        if reached:
            return text + "\n已通过"
        return text
