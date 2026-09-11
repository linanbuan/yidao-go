"""复盘页：六阶段进度 → 全盘报告 → 逐手讲解 → 导出 Markdown。

对应 `frontend/src/pages/ReviewPage.tsx`（445 行）。四条会算错的口径逐条抄它，
改之前先看清理由：
  · **棋盘上的圈只标我方**、且**此刻还留在盘上**、且已落到当前手之后的问题手 ——
    否则满屏红圈，与「AI 也下了恶手」两件事混在一处读不出来；
  · **曲线优先用 `report.curve`**：那是复盘时按 review_visits 重算的一版，比对局期
    实时推的那版准；没有它才退回 `analyses`（`build_curve` 与对局页共用一份实现）；
  · **只对 `status == 'pending'` 轮询**：`none` 是「从未入队」（强制结束的局），
    对它轮询会永远转圈，那种情况该给「立即生成」而不是进度条；
  · **报告里的 `ply` 与曲线的 `ply` 同一套编号**：`analyses[i]` = 第 i 手之后，
    而 `report.moves[k].ply == k+1`，所以点曲线第 p 个点 → 棋盘摆 p 手 →
    卡片正好是「第 p 手」。差一位就会全程讲解错位一手。

原生端四处**主动偏离**，各自写在所在处的注释里：
  ① 「胜率 / 目差」两行标上「（该行棋方）」—— 后端这两个字段取的是**下这一手那一方**
     的视角（analyzer._wr 传的是 `color` 而不是 player_color），网页版在 AI 的手上
     把它当「我方胜率」显示，学员读到的是反的；
  ② 「领地」开关在**这一局没有 ownership 数据**时整条不出现 —— 复盘完成后
     `rec.analyses` 会被不含 ownership 的高精度分析覆盖（worker.py:387 + 198），
     照抄网页版就会留一个点了没反应的开关；
  ③ 逐手列表用 QTableWidget 而不是每行一个自定义控件：两百多行 × 五个 QLabel
     在 Qt 里是真会卡的控件树，而表格天生就是干这个的；
  ④ 报告第一次到手时**停在「我方损失最大的那一手」**而不是第 0 手（见 `_default_ply`）。

 ply 与手数的另一件小事：曲线点数 = 手数 + 1（第 0 点是「还没落子」），
 这一条与计划里「断言曲线点数 == 手数」的字面写法差 1，按事实断言并记进日志，
 不为了对上文字而把第 0 点删掉 —— 删了它就等于把「开局时引擎怎么看这盘」抹掉。
"""
from __future__ import annotations

import math
import re
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFileDialog, QFrame, QGridLayout, QLabel,
    QProgressBar, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget, QHeaderView,
)

from core.api import err_text
from core.sound import SoundPlayer
from ui import theme
from ui.text import plain
from ui.widgets.board import GoBoard
from ui.widgets.parts import Alert, Panel, hbox
from ui.widgets import motion
from ui.widgets.summary import (
    SummaryPanel, first_clause, phase_tone, pivot_sentence, split_sentences,
    split_training,
)
from ui.widgets.winrate import WinrateChart

from .game import board_at, build_curve, color_name, last_move_of

EMPTY, BLACK, WHITE = 0, 1, 2

#: 评级 → 棋盘圈的颜色（网页版 FLAG_KIND 同一张表；good/pass 不上盘）
FLAG_KIND = {"blunder": "blunder", "bad": "bad", "slow": "slow", "good": "good"}
#: 曲线散点与「只看我方问题手」用的三种问题评级
PROBLEM_FLAGS = ("slow", "bad", "blunder")
#: 六阶段。顺序与后端 worker 的 STAGE_* 一致；**阶段名一律用接口下发的 stageText**，
#: 这张表只用来画「第几步 / 6」，后端加阶段最多是这个数字偏小，不会编出错文案。
STAGES = ("queued", "engine", "analyze", "report", "comment", "done")

#: 生成中的那句预期管理（网页版同文）。不写「请稍候」：等十几秒与等三分钟
#: 是两回事，学员要知道进度条停住时是自己这边在预热引擎，而不是程序卡了。
GEN_HINT = ("逐手重新分析是耗时大头：一盘 100 手的棋大约需要十几秒；刚启动服务时还要"
            "等 KataGo 预热（最多 3 分钟），进度条会停在「等引擎就绪」。")
LOW_CONF = ("精度提示：当前未使用 KataGo，胜率与损失目数来自内置启发式引擎，仅供粗略参考。"
            "在 backend 目录执行 python katago/download.py 安装引擎后重启后端，"
            "再点“重新生成”即可获得职业级分析。")
#: 右栏空着时的那句话。写成「会出现什么」而不是「请稍候」：学员知道等多久、
#: 等什么，才不会以为报告丢了而把程序关掉（同一理由见 GEN_HINT）。
WAIT_HINT = ("报告出来之后，这一栏会依次给出：整局总评、每一手的「为什么 / 怎么想」卡片、"
             "我方问题手列表。等待时可以先用左边的棋盘逐手回看这一盘。")
#: 轮询上限：1.2 秒一次 × 900 次 = 18 分钟。引擎等待最多 3 分钟、逐手分析十几秒，
#: 正常远到不了。**到顶不等于停手**（审计 M5）：从前一到上限就只重画、不再请求，
#: 界面永远停在「正在生成」，本进程内没有任何自愈路径；worker 死了就只能重启应用。
#: 现在是放缓到 `SLOW_POLL_MS` 继续问 —— 后端真跑完了照样能拿到，界面自己也说得清。
MAX_POLLS = 900
#: 超过上限之后的轮询间隔（10 秒）。改成慢问而不是不问：多打一个请求的代价
#: 远小于「永远停在正在生成、用户只能重启」。
SLOW_POLL_MS = 10000

#: 后端会带 Markdown 记号的散文字段。`comment.{reason,advice,maxim}` 与
#: `summary.*` 是大模型输出（模板那侧立了规矩只用「」，大模型没这约束），
#: `*.{detail,stageText}` 与 resultText 目前是纯中文，一并过一遍不亏。
PROSE_KEYS = frozenset({"reason", "advice", "maxim", "overall", "opening", "middle",
                        "endgame", "resultText", "detail", "stageText", "error",
                        "reviewError", "reviewDetail"})
#: 值是**字符串数组**的字段：`summary.training`（其余 list 都是数据点，不该动）
LIST_PROSE_KEYS = frozenset({"training"})


def clean(obj):
    """把一包服务端数据里的散文字段过一遍 `prose()`（就地改，返回同一对象）。

    与死活页同一做法：**收到数据时洗一次**，而不是在每个 setText 上各洗一遍 ——
    后者漏一处就是屏幕上多两颗星号，前者只要字段名对得上就漏不掉。
    """
    if isinstance(obj, dict):
        for key, value in list(obj.items()):
            if isinstance(value, str) and key in PROSE_KEYS:
                obj[key] = prose(value)
            elif isinstance(value, list) and key in LIST_PROSE_KEYS:
                obj[key] = [prose(v) if isinstance(v, str) else v for v in value]
            else:
                clean(value)
    elif isinstance(obj, list):
        for value in obj:
            clean(value)
    return obj


#: 行首的 Markdown 记号：标题井号、引用、无序/有序列表标记。
_HEAD = re.compile(r"^\s*(?:#{1,6}\s*|>\s*|[-*+]\s+|\d+[.)]\s+)")


def prose(text: str) -> str:
    """成对记号由 `plain()` 去，行首记号在这里去。

    `ui/text.py` 的 docstring 把「标题井号、列表星号这类行首记号」明确留给了
    复盘页：它是全站唯一会收到**整段**大模型输出的地方（`summary.overall`、
    `training[]`），而其他页面拿到的都是一句活。模型不按提示词里的
    「60~120 字」说话、而按它训练时的 Markdown 说话，这种情况不是假设。
    只去**行首**：正文中间一个 `*` 多半是「2*3」或强调，碰不得。
    """
    if not text:
        return text
    lines = [_HEAD.sub("", line) for line in plain(text).split("\n")]
    return "\n".join(lines).strip()


#: 总评面板的标题。抽出来是因为正文里可能重复它一句（见 `drop_lead`）。
PANEL_SUMMARY = "整局总评"
_SEP_HEAD = "：:，,。.、\n\r\t "


def drop_lead(text: str, label: str) -> str:
    """去掉正文开头那个与面板标题重复的引导词（「整局总评：这盘…」）。

    模板那侧不会写成这样，大模型会 —— 提示词里那个 JSON 键的说明就是
    `"overall": "整局总评：胜负关键…"`，模型照抄键名很常见。重复一遍标题
    不是排版事故，但学员会以为下面那段是另一个小节。只有紧跟分隔符才算引导词：
    「整局总评很重要」这种句子得原样留着。整段就是标题本身时也不剥（剥了变空）。
    """
    t = (text or "").strip()
    lab = (label or "").strip()
    if not lab or not t.startswith(lab):
        return t
    rest = t[len(lab):]
    if not rest.strip() or rest[0] not in _SEP_HEAD:
        return t
    return rest.lstrip(_SEP_HEAD)


def pct(value, digits: int = 1) -> str:
    """0~1 的比例 → 百分比文本。

    先乘到目标精度再加半个单位向下取整（half-up），而不是直接 `round()`：
    网页版用的是 `toFixed`（half-up），Python 的 round 是 half-even，
    48.5% 会两边差一格 —— 同一份数据两个界面差一个数字。"""
    if value is None:
        return "—"
    scale = 10 ** digits
    stepped = math.floor(float(value) * 100 * scale + 0.5) / scale
    return f"{stepped:.{digits}f}%"


def num(value, digits: int = 1, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}{suffix}"


def loss_color(loss) -> str:
    """损失目数的三档文字色（网页版 move-row 的 .loss 内联样式，同一个阈值）。"""
    v = float(loss or 0)
    if v >= 5:
        return "#c92a2a"
    if v >= 2:
        return "#e8590c"
    return "#495057"


def escape(text: str) -> str:
    """给 `Qt.RichText` 用的最小转义。PySide6 没绑出 `QString::toHtmlEscaped`，
    而这里只需三个字符，自己拼比引一套转换库划算。顺序不能换：`&` 先转，
    不然 `&lt;` 会被二次转义成 `&amp;lt;`。"""
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


class ReviewPage(QWidget):
    """AI 复盘页。`open_game(game_id)` 是唯一入口，`refresh()` 由外壳切页时调。"""

    #: 回大厅。做成信号而不是直接切页：只有外壳知道 QStackedWidget 在哪。
    backRequested = Signal()

    def __init__(self, api, sound: SoundPlayer | None = None, parent=None):
        super().__init__(parent)
        self._api = api
        self._sound = sound or SoundPlayer()
        self.game_id = ""
        self._reset()
        self._build_ui()
        # 进度轮询：单次触发 + 回包后再 start，避免上一包没回来就叠一发
        self._tick = QTimer(self)
        self._tick.setSingleShot(True)
        self._tick.setInterval(1200)
        self._tick.timeout.connect(self._poll_status)
        # ← → 逐手。Qt 的键盘事件发给**有焦点的控件**，页面默认拿不到焦点
        self.setFocusPolicy(Qt.StrongFocus)

    # ---------------------------------------------------------------- 状态

    def _reset(self) -> None:
        self.meta: dict = {}
        self.report: dict | None = None
        self.moves: list[dict] = []
        self.analyses: list[dict] = []
        self.prog: dict | None = None
        self.error = ""
        self.ply = 0
        self.busy = False
        self.show_variation = False
        self.only_problems = False
        self._loading = False
        self._list_dirty = False
        self._polls = 0
        self._slow = False        # 轮询超过 MAX_POLLS 之后的放缓档（审计 M5）
        self._landed = False      # 报告到手后已经选过第一眼看的那一手
        self._export_target = ""
        #: 手顺表的行签名（第 32 轮）：一样就不重建表格，见 `_paint_list`
        self._list_sig = None
        #: 最近一次成功取到的导出原文。写文件要弹保存框（测试里会把界面挂住），
        #: 所以取原文与落盘分开，验收断言这一项就够了（见 export_report_to）。
        self.last_export = ""
        self._reset_controls()

    def _reset_controls(self) -> None:
        """把三个开关摆回默认。**换局必须跟着摆**：上一局勾着「只看问题手」
        进下一局，数据已经换了而框还勾着，列表就少了一批手而不提示 ——
        学员会以为这一局没有缓手。`__init__` 里控件还没建，故逐个 getattr。"""
        for name in ("chkVariation", "chkOwnership", "chkOnlyProblems"):
            box = getattr(self, name, None)
            if box is not None:
                box.blockSignals(True)        # 换局时不要靠它的信号去重画（页面还没摆好）
                box.setChecked(False)
                box.blockSignals(False)

    # ---------------------------------------------------------------- 装配

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(10)
        root.addWidget(self._build_head())
        self.errorBar = Alert("err", "", self)
        root.addWidget(self.errorBar)
        root.addWidget(self._build_gen_panel())
        self.noneBar = Alert("warn", "立即生成", self)
        self.noneBar.button.clicked.connect(self._rerun)
        root.addWidget(self.noneBar)
        self.lowBar = Alert("warn", "", self)
        self.lowBar.label.setText(LOW_CONF)
        root.addWidget(self.lowBar)

        body = hbox(16)
        root.addLayout(body, 1)

        # ---------------- 左：棋盘 + 曲线。上限 620 是网页版 .grid-review 的
        # `minmax(360px, 620px)`；超过这个宽度继续放大棋盘只是晃眼。
        leftWrap = QWidget(self)
        leftWrap.setMinimumWidth(360)
        leftWrap.setMaximumWidth(620)
        lv = QVBoxLayout(leftWrap)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(10)
        lv.addWidget(self._build_board_panel(), 1)
        self.chartPanel = self._build_chart_panel()
        lv.addWidget(self.chartPanel)
        body.addWidget(leftWrap)

        # ---------------- 右：总评 + 逐手卡片 + 逐手列表（整列可滚）
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        side = QWidget()
        side.setMinimumWidth(330)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(0, 0, 8, 0)
        sv.setSpacing(12)
        self.summaryPanel = self._build_summary_panel()
        self.cardPanel = self._build_card_panel()
        self.waitHint = QLabel(WAIT_HINT, side)
        self.waitHint.setProperty("role", "muted")
        self.waitHint.setWordWrap(True)
        self.waitHint.setVisible(False)
        sv.addWidget(self.summaryPanel)
        sv.addWidget(self.cardPanel)
        sv.addWidget(self.waitHint)
        # 逐手列表吃掉右列剩下的全部高度（而不是加一条 `addStretch` 把空白留在底部）：
        # 空的地方本来就该给「行数最多」的那一栏。列表自己内部有滚动条，
        # 内容超高时整列照旧能滚 —— 两件事不冲突。
        sv.addWidget(self._build_list_panel(), 1)
        self.scroll.setWidget(side)
        body.addWidget(self.scroll, 1)

    def _build_head(self) -> QWidget:
        box = QWidget(self)
        h = hbox(8)
        h.setContentsMargins(0, 0, 0, 0)
        self.headTitle = QLabel("AI 复盘", box)
        self.headTitle.setProperty("role", "title")
        self.headSub = QLabel("", box)
        self.headSub.setProperty("role", "muted")
        self.btnBack = QPushButton("返回大厅", box)
        self.btnRerun = QPushButton("重新生成", box)
        self.btnExport = QPushButton("导出报告", box)
        for b in (self.btnBack, self.btnRerun, self.btnExport):
            b.setCursor(Qt.PointingHandCursor)
        self.btnBack.clicked.connect(self.backRequested.emit)
        self.btnRerun.clicked.connect(self._rerun)
        self.btnExport.clicked.connect(self._export)
        h.addWidget(self.headTitle)
        h.addWidget(self.headSub)
        h.addStretch(1)
        h.addWidget(self.btnBack)
        h.addWidget(self.btnRerun)
        h.addWidget(self.btnExport)
        box.setLayout(h)
        return box

    def _build_gen_panel(self) -> QWidget:
        """六阶段进度。默认收起，只有 pending 时出现。"""
        p = QFrame(self)
        p.setProperty("card", True)
        p.setStyleSheet(theme.alert_style("info"))
        v = QVBoxLayout(p)
        v.setContentsMargins(10, 8, 10, 10)
        v.setSpacing(6)
        self.genLabel = QLabel("复盘正在生成，完成后自动刷新…", p)
        self.genLabel.setProperty("role", "h3")
        self.genLabel.setStyleSheet("background: transparent; border: none;")
        self.genBar = QProgressBar(p)
        self.genBar.setRange(0, 100)
        self.genBar.setTextVisible(False)      # QSS 关不掉槽内文字，只能在这儿关
        self.genBar.setValue(0)
        self.genStage = QLabel("排队中", p)
        self.genStage.setWordWrap(True)
        self.genStage.setStyleSheet("background: transparent; border: none;")
        tip = QLabel(GEN_HINT, p)
        tip.setProperty("role", "muted")
        tip.setWordWrap(True)
        tip.setStyleSheet("background: transparent; border: none;")
        v.addWidget(self.genLabel)
        v.addWidget(self.genBar)
        v.addWidget(self.genStage)
        v.addWidget(tip)
        self.genPanel = p
        p.setVisible(False)
        return p

    def _build_board_panel(self) -> QWidget:
        p = Panel("", self)
        self.boardView = GoBoard(size=19, interactive=False, parent=p)
        p.body.addWidget(self.boardView, 1)

        row = hbox(8)
        self.btnStart = QPushButton("开局", p)
        self.btnPrev = QPushButton("◀ 上一手", p)
        self.btnNext = QPushButton("下一手 ▶", p)
        self.btnEnd = QPushButton("终局", p)
        self.plyBadge = QLabel("第 0 / 0 手", p)
        self.plyBadge.setProperty("role", "badge")
        self.plyBadge.setStyleSheet(theme.badge_style(""))
        for i, b in enumerate((self.btnStart, self.btnPrev, self.btnNext, self.btnEnd)):
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=i: self._step_to(k))
        row.addWidget(self.btnStart)
        row.addWidget(self.btnPrev)
        row.addWidget(self.btnNext)
        row.addWidget(self.btnEnd)
        row.addWidget(self.plyBadge)
        row.addStretch(1)
        p.body.addLayout(row)

        tgl = hbox(8)
        self.chkVariation = QCheckBox("变化图", p)
        self.chkVariation.toggled.connect(self._on_variation_toggled)
        self.chkOwnership = QCheckBox("领地", p)
        self.chkOwnership.toggled.connect(lambda *_a: self._paint_board())
        tgl.addWidget(self.chkVariation)
        tgl.addWidget(self.chkOwnership)
        tgl.addStretch(1)
        p.body.addLayout(tgl)

        self.judgeLine = QLabel("", p)
        self.judgeLine.setProperty("role", "muted")
        self.judgeLine.setWordWrap(True)
        p.body.addWidget(self.judgeLine)
        return p

    def _build_chart_panel(self) -> QWidget:
        p = Panel("", self)
        self.chart = WinrateChart(height=200, parent=p)
        self.chart.pointClicked.connect(self.set_ply)
        p.body.addWidget(self.chart, 1)
        hint = QLabel("红/橙/黄点分别为大恶手、恶手、缓手；曲线为我方视角，"
                      "点击任意位可跳到那一手", p)
        hint.setProperty("role", "muted")
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignCenter)
        p.body.addWidget(hint)
        return p

    def _build_summary_panel(self) -> QWidget:
        # 排版在 `ui/widgets/summary.py`：一句话一行、数字上卡、胜负分界抬成 callout。
        p = SummaryPanel(self)
        p.setVisible(False)
        return p

    def _build_card_panel(self) -> QWidget:
        p = Panel("", self)
        self.cardRows = QVBoxLayout()
        self.cardRows.setSpacing(6)
        p.body.addLayout(self.cardRows)
        p.setVisible(False)
        return p

    def _build_list_panel(self) -> QWidget:
        p = Panel("", self)
        head = hbox(8)
        t = QLabel("逐手分析", p)
        t.setProperty("role", "h3")
        self.chkOnlyProblems = QCheckBox("只看我方问题手", p)
        self.chkOnlyProblems.toggled.connect(self._on_filter_toggled)
        head.addWidget(t)
        head.addStretch(1)
        head.addWidget(self.chkOnlyProblems)
        p.body.addLayout(head)

        # QTableWidget 而不是「每行一个 QWidget」：见模块 docstring 第 ③ 条。
        self.table = QTableWidget(0, 4, p)
        self.table.setHorizontalHeaderLabels(["#", "手", "胜率", "损失"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setShowGrid(False)
        # 点一行之后焦点落在这里，← → 会被表格用成「移动单元格」：有 4 列，
        # 走到行尾会跳到下一行，读数就乱了。所以给 ClickFocus（让点击真的
        # 把焦点留在列表上，键盘不用先去别处绕一圈）再装事件过滤器拦下来 ——
        # 过滤器与 `eventFilter()` 是一对，少一边这条通路就是死的。
        self.table.setFocusPolicy(Qt.ClickFocus)
        self.table.installEventFilter(self)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.Fixed)
        hdr.setSectionResizeMode(3, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 44)
        self.table.setColumnWidth(2, 62)
        self.table.setColumnWidth(3, 78)
        self.table.setMinimumHeight(190)
        # 不设上限：这一栏是右列里唯一「有多少空就吃多少」的东西（用户报的
        # 「下面还有好大的空间，逐手分析只占一小栏」就是被 320px 卡出来的）。
        self.table.itemSelectionChanged.connect(self._on_pick_row)
        p.body.addWidget(self.table)
        self.emptyList = QLabel("没有匹配的手", p)
        self.emptyList.setProperty("role", "muted")
        self.emptyList.setVisible(False)
        p.body.addWidget(self.emptyList)
        tip = QLabel("提示：← → 逐手浏览；点击曲线可跳到对应手。", p)
        tip.setProperty("role", "muted")
        tip.setWordWrap(True)
        p.body.addWidget(tip)
        self.listPanel = p
        return p

    # ---------------------------------------------------------------- 数据

    def open_game(self, game_id: str) -> None:
        if not game_id:
            return
        self._tick.stop()
        self._reset()
        self.game_id = game_id
        self._paint_all()
        self._load()

    def refresh(self) -> None:
        """外壳切页时调。复盘报告不会自己变旧，但生成中的进度会变 —— 重取一次最省事。"""
        if self.game_id:
            self._load()

    def shutdown(self) -> None:
        self._tick.stop()

    def _status(self) -> str:
        """与网页版同一句：`prog?.status ?? meta.reviewStatus`。"""
        if self.prog and self.prog.get("status"):
            return str(self.prog["status"])
        return str(self.meta.get("reviewStatus") or "none")

    def _load(self) -> None:
        if not self.game_id:
            return
        if self._loading:
            self._list_dirty = True       # 在途时只记一次脏（与死活页同一招）
            return
        self._loading = True
        self._api.get(f"/api/reviews/{self.game_id}", timeout=40).finished.connect(
            self._on_payload)

    def _on_payload(self, data, err) -> None:
        self._loading = False
        if err is not None or not isinstance(data, dict):
            self.error = f"读取复盘失败：{err_text(err)}"
            self._paint_all()
            return
        gid = str((data.get("meta") or {}).get("gameId") or "")
        if gid and gid != self.game_id:
            return                        # 换局之后才回来的旧包，画上去就是串局
        self.meta = clean(data.get("meta") or {})
        report = data.get("report")
        self.report = clean(report) if isinstance(report, dict) else None
        self.moves = data.get("moves") or []
        self.analyses = data.get("analyses") or []
        self.error = ""
        self.ply = max(0, min(self.ply, len(self.moves)))
        # 第一眼停在哪一手：只在「报告第一次到手」那一瞬定一次，之后用户
        # 拖到哪儿都不再抢他的位置（轮询重取、切页重取都不会把他拽回去）。
        if self.report and not self._landed and self.ply == 0:
            self.ply = self._default_ply()
        self._landed = bool(self.report) or self._landed
        self._paint_all()
        self._sync_polling()
        if self._list_dirty:
            self._list_dirty = False
            self._load()

    def _poll_status(self) -> None:
        if not self.game_id:
            return
        self._polls += 1
        if self._polls == MAX_POLLS + 1:
            # 到顶了：放缓继续问，并把这件事说出来（审计 M5）。不再 `return` 收工 ——
            # 那条路径是「永远停在正在生成、且本进程内无法自愈」。
            self._slow = True
            self._paint_alerts()
        self._api.get(f"/api/reviews/{self.game_id}/status").finished.connect(
            self._on_status)

    def _on_status(self, data, err) -> None:
        if err is not None or not isinstance(data, dict):
            # 瞬时网络错不打扰用户，3 秒后再试（与网页版同参数）；超时档同样放缓
            if self.game_id:
                self._tick.start(3000 if self._polls <= MAX_POLLS else SLOW_POLL_MS)
            return
        if str(data.get("gameId") or "") != self.game_id:
            return
        self.prog = clean(data)
        status = str(data.get("status") or "")
        if status == "pending":
            # 还在排队/生成：续一个定时器，**只重画进度那一块**。整页重建（总评 ~40
            # 个控件 + 曲线）在 1.2 秒一次的节奏上是可见卡顿与耗电的来源（审计 M9）。
            self._tick.start(1200 if self._polls <= MAX_POLLS else SLOW_POLL_MS)
            self._paint_alerts()
            return
        if status in ("done", "failed"):
            if status == "done":
                self._sound.play("review")
            # 完成与失败都要重取一次整份报告：完成才有内容，失败要把 reviewError
            # 带回那条「暂无复盘报告」（不重取就只能显示进页那一刻的旧值）。
            self._load()
        self._paint_all()

    def _sync_polling(self) -> None:
        if self._status() == "pending" and not self._tick.isActive():
            self._tick.start(1200)

    def _rerun(self, *_a) -> None:
        """重新生成（POST 后重取一次，进度条由轮询接手）。"""
        if not self.game_id or self.busy:
            return
        self.busy = True
        self._paint_buttons()
        self._api.post(f"/api/reviews/{self.game_id}").finished.connect(self._on_rerun)

    def _on_rerun(self, data, err) -> None:
        self.busy = False
        if err is not None:
            self.error = f"重新生成没能入队：{err_text(err)}"
        else:
            self.error = ""
            self.prog = None              # 旧的进度会盖住服务端刚写的 0%
            self._polls = 0
        self._paint_all()
        self._paint_buttons()
        if err is None:
            self._load()

    # ---------------------------------------------------------------- 导出

    def _export(self) -> None:
        if not self.report:
            return
        name = f"review_{self.game_id[:8]}.md"
        path, _sel = QFileDialog.getSaveFileName(self, "导出复盘报告", name,
                                                "Markdown (*.md);;所有文件 (*)")
        if path:
            self.export_report_to(path)

    def export_report_to(self, path: str) -> bool:
        """把报告落成 Markdown。原文一律由后端出（`review_to_markdown`），
        客户端不重排一遍 —— 两个客户端各排一套，导出就会分叉。"""
        if not self.game_id:
            return False
        self._export_target = str(path)
        self._api.get_text(f"/api/reviews/{self.game_id}/export").finished.connect(
            self._on_export)
        return True

    def _on_export(self, data, err) -> None:
        target = getattr(self, "_export_target", "")
        self._export_target = ""
        if err is not None or not isinstance(data, str) or not data.strip():
            self.error = f"导出失败：{err_text(err) or '报告还是空的'}"
            self._paint_all()
            return
        self.last_export = data
        if not target:
            return
        try:
            Path(target).write_text(data, encoding="utf-8")
        except OSError as exc:
            self.error = f"写入 {target} 失败：{exc}"
            self._paint_all()
            return
        self.error = ""
        self._paint_all()

    # ---------------------------------------------------------------- 派生数据

    def player_color(self) -> int:
        return int(self.meta.get("playerColor") or BLACK)

    def _analysis(self, ply: int) -> dict | None:
        a = self.analyses[ply] if 0 <= ply < len(self.analyses) else None
        return a if a and not a.get("missing") else None

    def _curve(self) -> list[dict]:
        rep = self.report or {}
        curve = rep.get("curve")
        if curve:
            return list(curve)
        return build_curve(self.moves, self.analyses)

    def _selected(self) -> dict | None:
        for m in ((self.report or {}).get("moves") or []):
            if int(m.get("ply") or 0) == self.ply:
                return m
        return None

    def _default_ply(self) -> int:
        """报告到手时停在哪一手：**我方损失最大的那一手**（偏离④）。

        网页版停在第 0 手 —— 那是一张空盘加一张没有卡片的右栏：学员从对局页点
        「生成 AI 复盘」进来，看见的第一眼什么都不是，还得自己拖到出问题那一手。
        复盘页存在的理由就是「看我这手为什么错」，第一眼就该是它。
        一局没有缓手以上的问题时停在终局（那是他下的最后一个局面）。
        """
        mine = [m for m in ((self.report or {}).get("moves") or [])
                if m.get("isPlayer") and m.get("flag") in PROBLEM_FLAGS]
        if not mine:
            return len(self.moves)
        worst = max(mine, key=lambda m: float(m.get("lossPoints") or 0.0))
        return int(worst.get("ply") or 0)

    def _board(self) -> list[list[int]]:
        return board_at(int(self.meta.get("size") or 19), self.moves, self.ply,
                        int(self.meta.get("handicap") or 0))

    def _marks(self) -> list[dict]:
        board = self._board()
        size = len(board)
        out = []
        for m in ((self.report or {}).get("moves") or []):
            x, y = m.get("x"), m.get("y")
            flag = m.get("flag")
            if not m.get("isPlayer") or flag not in FLAG_KIND or flag == "good":
                continue
            if x is None or y is None or int(m.get("moveNum") or 0) > self.ply:
                continue
            if not (0 <= int(x) < size and 0 <= int(y) < size):
                continue
            if board[int(y)][int(x)] == EMPTY:
                continue            # 已被提掉的子不再画圈：空点上的圈像个 bug
            out.append({"x": int(x), "y": int(y), "kind": FLAG_KIND[flag],
                        "label": str(m.get("moveNum"))})
        return out

    def _variation(self) -> list[dict]:
        """引擎首选的变化线（幽灵子）。

        **从这一手自己的颜色开始**（审计 1.19）：`variation` 是 `bestMove.pvPoints`，
        而 pv 的第 0 个就是引擎推荐的那一手本身（`commentary.py` 与
        `engine/protocol.py:137` 同一口径：`pv[0]` = 首选）。这一手是谁下的、
        引擎的替代着就还是谁下，此后照常交替。
        从前从**对手色**起画，整条线的黑白全反 —— 卡片上写着「引擎首选 e3」，
        盘上却用白子标了个「1」，学员照着摆会摆出一串错色的子。
        """
        sel = self._selected()
        if not self.show_variation or not sel:
            return []
        out = []
        color = int(sel.get("color") or BLACK)
        for i, point in enumerate(sel.get("variation") or []):
            if not point or point[0] is None or point[1] is None:
                continue
            out.append({"x": int(point[0]), "y": int(point[1]), "color": color,
                        "label": str(i + 1)})
            color = WHITE if color == BLACK else BLACK
        return out

    def _chart_marks(self) -> list[dict]:
        return [{"ply": int(m.get("ply")), "kind": m.get("flag")}
                for m in ((self.report or {}).get("moves") or [])
                if m.get("isPlayer") and m.get("flag") in PROBLEM_FLAGS
                and m.get("ply") is not None]

    def _listed_moves(self) -> list[dict]:
        rows = (self.report or {}).get("moves") or []
        if self.only_problems:
            return [m for m in rows if m.get("isPlayer") and m.get("flag") in PROBLEM_FLAGS]
        return list(rows)

    def _has_ownership(self) -> bool:
        """这一局的分析里到底有没有领地数据。没有就不摆那个开关（见模块 ②）。"""
        return any(isinstance(a, dict) and a.get("ownership") for a in self.analyses)

    # ---------------------------------------------------------------- 导航

    def set_ply(self, ply) -> None:
        try:
            value = max(0, min(len(self.moves), int(ply)))
        except (TypeError, ValueError):
            return
        if value == self.ply:
            return
        self.ply = value
        self._paint_board()
        self._paint_chart_current()
        self._paint_card()
        self._select_row()

    def step(self, delta: int) -> None:
        self.set_ply(self.ply + delta)

    def _step_to(self, which: int) -> None:
        """开局 / 上一手 / 下一手 / 终局 四个按钮共用一个槽（见装配处的 lambda）。"""
        if which == 0:
            self.set_ply(0)
        elif which == 1:
            self.step(-1)
        elif which == 2:
            self.step(1)
        else:
            self.set_ply(len(self.moves))

    def _on_variation_toggled(self, on: bool) -> None:
        self.show_variation = bool(on)
        self._paint_board()

    def _on_filter_toggled(self, *_a) -> None:
        self.only_problems = bool(self.chkOnlyProblems.isChecked())
        self._paint_list()
        self._select_row()

    def _on_pick_row(self) -> None:
        if getattr(self, "_syncing_row", False):
            return
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.table.item(row, 0)
        ply = item.data(Qt.UserRole) if item is not None else None
        if ply is not None:
            self.set_ply(ply)

    def _select_row(self) -> None:
        """把当前手那一行选上。`_syncing_row` 挡住一次回环：
        setCurrentCell → itemSelectionChanged → set_ply → _select_row。"""
        self._syncing_row = True
        try:
            found = -1
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item is not None and item.data(Qt.UserRole) == self.ply:
                    found = row
                    break
            if found >= 0 and self.table.currentRow() != found:
                self.table.setCurrentCell(found, 0)
                self._follow_row(found)
        finally:
            self._syncing_row = False

    def _follow_row(self, row: int) -> None:
        """让选中行**滑**进视野（第 32 轮动效）。

        `setCurrentCell` 只会把行"跳"进来：逐手回看时表格一卡一卡地蹦。
        这里改成动画滚动条的值 —— 滚动条在控件内部变化，不触发任何重排，
        是代价最小的那类动效。动效关掉时就是直接到位（与从前行为一致）。
        """
        bar = self.table.verticalScrollBar()
        target = bar.value()
        item = self.table.item(row, 0)
        if item is not None:
            top = self.table.rowViewportPosition(row)
            height = self.table.rowHeight(row)
            view = self.table.viewport().height()
            if top < 0:
                target = max(bar.minimum(), bar.value() + top)
            elif top + height > view:
                target = min(bar.maximum(), bar.value() + top + height - view)
        motion.animate_value(bar, "value", target, "scroll")

    def keyPressEvent(self, ev):                              # noqa: N802
        if ev.key() == Qt.Key_Left:
            self.step(-1)
        elif ev.key() == Qt.Key_Right:
            self.step(1)
        else:
            super().keyPressEvent(ev)

    def eventFilter(self, obj, ev):                           # noqa: N802
        """表格自己会把 ← → 用成「移动当前单元格」，而那恰好等于逐手 ——
        但列有 4 个，走到行尾会跳到下一行，读数就乱了。拦下来交给同一个 step()。"""
        if obj is self.table and ev.type() == QEvent.KeyPress \
                and ev.key() in (Qt.Key_Left, Qt.Key_Right):
            self.step(-1 if ev.key() == Qt.Key_Left else 1)
            return True
        return super().eventFilter(obj, ev)

    # ---------------------------------------------------------------- 绘制

    def _paint_all(self) -> None:
        self._paint_head()
        self._paint_alerts()
        self._paint_buttons()
        self._paint_board()
        self._paint_chart()
        self._paint_summary()
        self._paint_card()
        self._paint_list()
        self._select_row()

    def _paint_head(self) -> None:
        if not self.meta:
            self.headTitle.setText("AI 复盘")
            self.headSub.setText("从大厅的「对局记录」里选一局开始")
            return
        self.headTitle.setText(f"AI 复盘 · {self.meta.get('rankName')}"
                               if self.meta.get("rankName") else "AI 复盘")
        self.headSub.setText(f"对阵 {self.meta.get('aiName') or 'AI'}"
                             f"　{self.meta.get('size', 19)}路"
                             f"　我执{color_name(self.player_color())}")

    def _paint_alerts(self) -> None:
        self.errorBar.show_text(self.error, "err")
        generating = self._status() == "pending"
        self.genPanel.setVisible(generating)
        if generating:
            prog = self.prog or {}
            frac = float(prog.get("progress")
                         if prog.get("progress") is not None
                         else (self.meta.get("reviewProgress") or 0.0))
            stage = str(prog.get("stage") or self.meta.get("reviewStage") or "")
            index = STAGES.index(stage) + 1 if stage in STAGES else 0
            name = str(prog.get("stageText") or self.meta.get("reviewDetail") or "排队中")
            # 进度条走格而不是跳格（生成中每 1.2 秒轮询一次）；动效关掉时是直接赋值
            motion.animate_value(self.genBar, "value", int(frac * 100 + 0.5), "bar")
            self.genStage.setText(f"第 {index}/{len(STAGES)} 步 · {name} · "
                                  f"{int(frac * 100 + 0.5)}%"
                                  + (f"　{prog.get('detail')}" if prog.get("detail") else "")
                                  # 等太久了要说清"还在问、只是问得慢了"，
                                  # 而不是让界面看起来已经卡死（审计 M5）
                                  + ("　（等待超过 18 分钟，已放慢重试；"
                                     "仍无进展时可到设置页看日志或点「重新生成」）"
                                     if self._slow else ""))
        # 「暂无报告」那条：生成中不出现（那时上面一条已经说了在等什么）
        show_none = not generating and not self.report and bool(self.game_id)
        reason = self.meta.get("reviewError") or (self.prog or {}).get("error") or ""
        self.noneBar.show_text(f"暂无复盘报告{f'：{reason}' if reason else ''}"
                               if show_none else "", "warn")
        self.lowBar.setVisible(bool(self.report and self.report.get("lowConfidence")))
        # 没有报告时右栏三个面板都收起了，整个右半边是一片空底 —— 原生窗口不像
        # 网页那样有背景可看，缺一块就是缺一块。留一句「等一下会出现什么」。
        self.waitHint.setVisible(not self.report and bool(self.game_id))

    def _paint_buttons(self) -> None:
        self.btnRerun.setEnabled(not self.busy and bool(self.game_id))
        self.btnRerun.setText("正在提交…" if self.busy else "重新生成")
        self.btnExport.setEnabled(bool(self.report))
        has = bool(self.moves)
        for b in (self.btnStart, self.btnPrev, self.btnEnd):
            b.setEnabled(has)
        self.btnPrev.setEnabled(has and self.ply > 0)
        self.btnStart.setEnabled(has and self.ply > 0)
        self.btnNext.setEnabled(has and self.ply < len(self.moves))
        self.btnEnd.setEnabled(has and self.ply < len(self.moves))
        self.chkVariation.setEnabled(bool((self._selected() or {}).get("variation")))

    def _paint_board(self) -> None:
        size = int(self.meta.get("size") or 19)
        if self.boardView.board_size != size:
            self.boardView.set_size(size)
        board = self._board()
        own = (self._analysis(self.ply) or {}).get("ownership")
        show_own = bool(self.chkOwnership.isChecked()) and bool(own)
        self.chkOwnership.setVisible(bool(self._has_ownership()))
        self.boardView.set_props(board=board,
                                 last_move=last_move_of(self.moves, self.ply),
                                 moves=self.moves[:self.ply],
                                 show_move_numbers=self.ply > 0,
                                 marks=self._marks(), variation=self._variation(),
                                 ownership=own, show_ownership=show_own,
                                 hints=[], interactive=False)
        self.plyBadge.setText(f"第 {self.ply} / {len(self.moves)} 手")
        self.judgeLine.setText(self._judge_text())
        self._paint_buttons()

    def _judge_text(self) -> str:
        """形势判断：当前这个局面上引擎怎么看。放在棋盘下面而不是曲线里 ——
        它是「此刻这一步」的读数，跟棋盘是同一件事。"""
        a = self._analysis(self.ply)
        head = (f"形势判断（第 {self.ply} 手后）" if self.ply
                else "形势判断（开局）")
        if not a:
            # 两种「没有数据」不是一回事：整局都没分析（复盘还没跑完 / 跑失败了）
            # 与只有这一手没取到。对前者说「这一手没有引擎分析数据」既是错的口径
            # （第 0 手根本没有「这一手」），也把用户往错的地方支走。
            if not any(self._analysis(i) for i in range(len(self.analyses))):
                return (f"{head}：这一局还没有引擎分析数据"
                        f"（复盘未完成或未取到），只看局面。")
            return f"{head}：这一手没有引擎分析数据（复盘时未取到），只看局面。"
        color = self.player_color()
        key = "winrateBlack" if color == BLACK else "winrateWhite"
        wr = a.get(key)
        lead = a.get("scoreLead")
        lead = None if lead is None else (float(lead) if color == BLACK else -float(lead))
        parts = [f"我方胜率 {pct(wr)}" if wr is not None else "我方胜率 —"]
        if lead is None:
            parts.append("目差 —")
        else:
            parts.append(f"目差 {lead:+.1f}（{'领先' if lead > 0 else '落后' if lead < 0 else '持平'}）")
        visits = a.get("visits")
        if visits:
            parts.append(f"{visits} 次推演")
        return f"{head}：" + " · ".join(parts)

    def _paint_chart(self) -> None:
        curve = self._curve()
        self.chart.setVisible(bool(curve))
        self.chartPanel.setVisible(bool(curve))
        if curve:
            self.chart.set_data(curve, self._chart_marks(), self.player_color())
        self._paint_chart_current()

    def _paint_chart_current(self) -> None:
        self.chart.set_current(self.ply)

    def _paint_summary(self) -> None:
        rep = self.report
        self.summaryPanel.setVisible(bool(rep))
        if not rep:
            return
        s = rep.get("summary") or {}
        counts = rep.get("counts") or {}

        # 一句话一行：整段墙按句号切开（见 summary.py 的口径），最该看见的那句抬成 callout。
        pivot, sentences = pivot_sentence(
            split_sentences(drop_lead(str(s.get("overall") or ""), PANEL_SUMMARY)))

        # 三阶段：有结构化数据就摆数字卡，只有散文（大模型写的）就原样退回两列。
        cards: list[dict] = []
        notes: list[str] = []
        fallback: list[tuple[str, str]] = []
        phases = rep.get("phases") or {}
        for key, label in (("opening", "布局"), ("middle", "中盘"), ("endgame", "官子")):
            p = phases.get(key) or {}
            text = str(s.get(key) or "")
            if p.get("avgLoss") is not None:
                avg = p.get("avgLoss")
                cards.append({
                    "label": label,
                    "moves": p.get("moves", 0),
                    "avg": num(avg, 1),
                    "avgColor": loss_color(avg),
                    "worst": num(p.get("worstLoss"), 1),
                    "worstNum": p.get("worstMoveNum") or "—",
                })
                tone = phase_tone(text)
                if tone:
                    notes.append(f"{label} {first_clause(tone)}")
            elif text:
                fallback.append((label, text))

        training = []
        for item in (s.get("training") or []):
            if not item:
                continue
            lead, body = split_training(str(item))
            training.append((lead, body))

        engine = "KataGo" if rep.get("engine") == "katago" else "内置启发式引擎"
        llm = rep.get("llm") or {}
        told = f"大模型 {llm.get('model')}" if llm.get("used") \
            else f"模板生成（{llm.get('error') or '未配置大模型'}）"

        self.summaryPanel.render({
            "title": PANEL_SUMMARY,
            "result": str(rep.get("resultText") or ""),
            "gauges": [(num(rep.get("avgLossPoints"), 2), "我方吻合度"),
                       (num(rep.get("aiAvgLossPoints"), 2), "AI 吻合度"),
                       (str(rep.get("totalMoves", "—")), "总手数")],
            "counts": [("大恶手", counts.get("blunder", 0), "blunder"),
                       ("恶手", counts.get("bad", 0), "bad"),
                       ("缓手", counts.get("slow", 0), "slow"),
                       ("好手", counts.get("good", 0), "good")],
            "sentences": sentences,
            "pivot": pivot,
            "phases": cards,
            "phase_notes": "　·　".join(notes),
            "phase_fallback": fallback,
            "training": training,
            "maxim": s.get("maxim"),
            "foot": (f"数据来源：{engine}（每手 {rep.get('visits', '—')} 次推演）"
                     f"　·　讲解：{told}"),
        })

    def _maxim(self, text: str, parent: QWidget | None = None) -> QLabel:
        lab = QLabel(text, parent or self.summaryPanel)
        lab.setStyleSheet("color: #846a06; font-style: italic; background: transparent;"
                          " border: none;")
        lab.setWordWrap(True)
        return lab

    def _badge(self, text: str, flag: str = "", kind: str = "") -> QLabel:
        """一枚徽章。给了评级就按评级上色（`.badge.flag-*`），否则用 `role` 那套语义色。"""
        lab = QLabel(text, self)
        lab.setProperty("role", "badge")
        lab.setStyleSheet(theme.flag_badge_style(flag) if flag
                          else theme.badge_style(kind))
        return lab

    def _row(self, layout, widget: QWidget) -> None:
        layout.addWidget(widget)

    def _grid(self, parent: QWidget) -> QGridLayout:
        """两列 kv。值列必须给 stretch：不给的话长文本会把整列撑到比侧栏还宽，
        而横向滚动条是关着的 —— 结果只是尾部被默默切掉，没有省略号。"""
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)
        grid.setColumnStretch(1, 1)
        grid.setColumnMinimumWidth(0, 92)
        return grid

    def _kv(self, grid: QGridLayout, row: int, cap: str, value: str) -> None:
        k = QLabel(cap)
        k.setProperty("role", "muted")
        v = QLabel(value)
        v.setWordWrap(True)
        grid.addWidget(k, row, 0)
        grid.addWidget(v, row, 1)

    def _paint_card(self) -> None:
        sel = self._selected()
        self.cardPanel.setVisible(bool(sel))
        self._paint_buttons()
        if not sel:
            return
        self._drain(self.cardRows)

        head = hbox(8)
        who = "我方" if sel.get("isPlayer") else "AI"
        title = QLabel(f"第 {sel.get('moveNum')} 手 · {who}"
                       f"（{color_name(int(sel.get('color') or 0))}）"
                       f"{sel.get('gtp') or ''}", self.cardPanel)
        title.setProperty("role", "h3")
        title.setWordWrap(True)
        head.addWidget(title)
        head.addWidget(self._badge(str(sel.get("flagLabel") or ""),
                                   str(sel.get("flag") or "")))
        head.addStretch(1)
        self.cardRows.addLayout(head)

        grid = self._grid(self.cardPanel)
        wr_note = "（该行棋方）"      # 见模块 docstring 第 ① 条
        best = sel.get("bestMove") or {}
        rank = sel.get("playerRank")
        rows = [
            (f"胜率{wr_note}", f"{pct(sel.get('winrateBefore'))} → "
                              f"{pct(sel.get('winrateAfter'))}"),
            (f"目差{wr_note}", f"{num(sel.get('scoreBefore'), 1)} → "
                              f"{num(sel.get('scoreAfter'), 1)}"),
            ("损失", (f"{sel.get('lossPoints')} 目" if sel.get("lossPoints") is not None
                     else "—")
                    + (f"（胜率 -{pct(sel.get('lossWinrate'))}）"
                       if sel.get("lossWinrate") else "")),
            ("引擎首选", str(best.get("gtp") or "—")
                       + (f"　·　你的落点排在候选第 {int(rank) + 1} 位"
                          if rank is not None else "")),
        ]
        pv = [str(v) for v in (sel.get("variationGtp") or [])][:8]
        if pv:
            rows.append(("参考变化", " ".join(pv)))
        for i, (cap, value) in enumerate(rows):
            self._kv(grid, i, cap, value)
        self.cardRows.addLayout(grid)

        comment = sel.get("comment")
        if isinstance(comment, dict) and comment:
            box = QFrame(self.cardPanel)
            box.setProperty("card", True)
            box.setStyleSheet("background: #fcfcfd; border: 1px solid "
                              f"{theme.LINE}; border-radius: 10px;")
            bv = QVBoxLayout(box)
            bv.setContentsMargins(12, 10, 12, 10)
            bv.setSpacing(6)
            for cap, key in (("为什么：", "reason"), ("怎么想：", "advice")):
                text = str(comment.get(key) or "")
                if text:
                    bv.addWidget(self._rich_line(cap, text, box))
            if comment.get("maxim"):
                bv.addWidget(self._maxim(f"「{comment['maxim']}」", box))
            src = QLabel(f"讲解来源：{'大模型' if comment.get('source') == 'llm' else '模板'}",
                         box)
            src.setProperty("role", "muted")
            src.setStyleSheet("background: transparent; border: none;")
            bv.addWidget(src)
            self.cardRows.addWidget(box)
            return
        line = QLabel(f"这一手没有明显问题，引擎判定为{sel.get('flagLabel') or '好手'}。",
                      self.cardPanel)
        line.setProperty("role", "muted")
        line.setWordWrap(True)
        self.cardRows.addWidget(line)

    def _rich_line(self, cap: str, text: str, parent: QWidget) -> QLabel:
        """一行正文，只把**标签**加粗。

        用富文本而不是拼 Markdown：后端送来的 `**粗**` 已由 `plain()` 洗掉，
        这里剩下的是我们自己排的版面（「为什么：」加粗），不借 Markdown 之便，
        免得将来正文里又多出一对星号。正文一律先转义再拼：
        引擎文本里出现 `<` / `&` 时，不转义会被当成标签半句吃掉（少一句讲解、
        不报错也不提示）。
        """
        line = QLabel(parent=parent)
        line.setTextFormat(Qt.RichText)
        line.setText(f"<b>{cap}</b>{escape(text)}")
        line.setWordWrap(True)
        line.setStyleSheet("background: transparent; border: none;")
        return line

    def _paint_list(self) -> None:
        rows = self._listed_moves()
        # 整块面板只在**真有一手可列**时出现：报告还没生成时摆一张空表格，
        # 配一句「没有匹配的手」，读起来像是筛选把东西滤没了，而实情是根本没数据。
        self.listPanel.setVisible(bool((self.report or {}).get("moves")))
        self.emptyList.setText(
            "这一局我方没有缓手以上的失误" if (self.only_problems and not rows) else "没有匹配的手")
        # 表格只在**行集合真的变了**时重建（第 32 轮）。逐手回看（← →）会走
        # `_paint_all`，而 300 手重建一次表格实测 11.8 ms —— 每按一下方向键都重建
        # 一张一模一样的表，是复盘页最明显的那处顿。签名把**画进格子的每一项**都算上
        # （#手数/我方AI/坐标/评级/胜率/损失）：少算一项，重新生成报告后就会留着旧字。
        sig = (len(rows), self.only_problems,
               tuple((m.get("ply"), m.get("moveNum"), m.get("isPlayer"), m.get("color"),
                      m.get("gtp"), m.get("flagLabel"), m.get("flag"),
                      m.get("winrateAfter"), m.get("lossPoints")) for m in rows))
        if sig == self._list_sig:
            self.table.setVisible(bool(rows))
            self.emptyList.setVisible(not rows)
            return
        self._list_sig = sig
        # 重建期间不让选行回抛：`clearContents`/`setRowCount` 会动到当前行，
        # 那一瞬拿到的旧 item 会把 ply 跳到一个根本没选中的手上。
        self._syncing_row = True
        self.table.setUpdatesEnabled(False)
        try:
            self.table.clearContents()
            self.table.setRowCount(len(rows))
            for i, m in enumerate(rows):
                ply = int(m.get("ply") or 0)
                who = "我" if m.get("isPlayer") else "AI"
                cells = (f"#{m.get('moveNum')}",
                         f"{who}（{color_name(int(m.get('color') or 0))}）"
                         f"{m.get('gtp') or ''} {m.get('flagLabel') or ''}".strip(),
                         pct(m.get("winrateAfter"), 0),
                         "—" if m.get("lossPoints") is None else f"-{m.get('lossPoints')} 目")
                for col, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    if col == 0:
                        item.setData(Qt.UserRole, ply)
                    if col == 1:
                        item.setForeground(QColor(theme.FLAG_COLOR.get(
                            str(m.get("flag") or ""), theme.INK)))
                    if col == 3:
                        item.setForeground(QColor(loss_color(m.get("lossPoints"))))
                    self.table.setItem(i, col, item)
        finally:
            # 任何一行数据异常都不能把表格永久冻住（审计 L2）：`setUpdatesEnabled(False)`
            # 不还回去 = 表格定格，`_syncing_row` 卡在 True = 选行再也不回抛。
            self.table.setUpdatesEnabled(True)
            self._syncing_row = False
        self.table.setVisible(bool(rows))
        self.emptyList.setVisible(not rows)

    @staticmethod
    def _drain(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout() is not None:
                ReviewPage._drain(item.layout())
