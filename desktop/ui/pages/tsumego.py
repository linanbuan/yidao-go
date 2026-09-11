"""死活练习页：筛选出题 → 落子 → 服务端判定 → 正解线演示 + 练习统计。

对应 frontend/src/pages/TsumegoPage.tsx。五张表（题型、目标、目标提示语、
题库分组顺序、难度档）逐条抄自它：后端只认 kind/goal/tier 三个参数名，
选项文案与顺序全在客户端，抄漏一项就是一个筛不出的选项。

判定一律交给服务端（backend/app/api/tsumego.py），两个理由都还成立：
  · 答案变化线 lines 不下发，打开调试工具也抄不到；
  · 提子、劫、自杀这些规则细节由规则引擎统一处理，接口把判定**之后**的棋盘
    整个回传，客户端照着画就行 —— 原生端因此不必再实现一遍围棋规则。
    （`ui/widgets/board.py` 只管画，这页一行业务规则都不判定。）
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from core.sound import SoundPlayer
from ui.text import plain
from ui.widgets.board import GoBoard
from ui.widgets.parts import Alert, ElidedLabel, Panel, hbox
from ui.widgets import motion

from .. import theme

EMPTY, BLACK, WHITE = 0, 1, 2
#: 判定结果。字符串与后端 tsumego.py 的 STATUS_* 一致，别改成枚举序号。
SOLVED, FAILED, CONTINUE = "solved", "failed", "continue"

#: 答对之后自动换下一题的等待时间。要够看完「✓ 正解！」和棋盘上的正解圈、
#: 听完那声 correct，又要短到不觉得卡 —— 1.6 秒是这两条的交点。
AUTO_NEXT_MS = 1600

#: 题型筛选。与后端 library.KIND_TEXT 一一对应（后端 KIND_PATTERN 还认 seki 键，
#: 但题库没有 seki 题型——双活只当结论不当题型，列出来就是一个筛出空列表的死选项）。
KINDS: tuple[tuple[str, str], ...] = (
    ("", "全部题型"), ("life", "死活"), ("ko", "劫争"),
    ("race", "对杀"), ("capture", "吃子手筋"), ("connect", "连络切断"),
)

#: 目标选项跟着题型走：列全九个目标会让「吃子 + 做活」这种空组合可被选中。
GOALS_BY_KIND: dict[str, tuple[tuple[str, str], ...]] = {
    "": (("", "全部目标"), ("live", "做活"), ("kill", "杀棋"), ("ko_kill", "劫杀"),
         ("race", "对杀取胜"), ("capture", "吃子"), ("connect", "连络"), ("cut", "切断")),
    "life": (("", "全部目标"), ("live", "做活"), ("kill", "杀棋")),
    "ko": (("", "全部目标"), ("ko_kill", "劫杀"),
           # 题库里暂时没有劫活题，这一项会被 _goal_options() 剪掉（见它的注释）
           ("ko_live", "劫活")),
    "race": (("", "全部目标"), ("race", "对杀取胜")),
    "capture": (("", "全部目标"), ("capture", "吃子")),
    # 连络/切断：L3 上线（pv 脱先哨兵到位），两个目标都出题。
    "connect": (("", "全部目标"), ("connect", "连络"), ("cut", "切断")),
}

#: 目标 → 一句话任务描述（侧栏「本题」面板用）。
GOAL_HINT: dict[str, str] = {
    "live": "做出两个真眼活棋",
    "kill": "破眼杀死对方",
    "ko_live": "打劫求活（这个形净活做不到）",
    "ko_kill": "点入打成劫杀（这个形净杀做不到）",
    "seki": "做成双活（谁先紧气谁死）",
    "race": "比气对杀，先把对方提光",
    "capture": "吃掉标了蓝圈的目标子",
    "connect": "把标了紫圈的两块棋连成一块",
    "cut": "占住要点，让对方的两块棋永远连不上",
}

#: 题库列表的题型分组顺序（没列到的排在最后）。
KIND_ORDER: tuple[str, ...] = ("死活", "劫争", "对杀", "吃子手筋", "双活")

#: 难度档下拉的选项（与后端 library.TIERS 一致）。按档而不按 1~9 的数字：
#: 学员想的是「做中级的」，而且后端已经按这个顺序统计好了。
TIERS: tuple[str, ...] = ("入门", "初级", "中级", "高级", "段位")

#: 判定结果 → 提示框第一行。文案抄网页版（✓ / ✗ / … 三个符号都算信息）。
RESULT_HEAD = {SOLVED: "✓ 正解！", FAILED: "✗ 不成立", CONTINUE: "… 对，继续应对"}


def setup_board(problem: dict) -> list[list[int]]:
    """按题目的 setup 摆初始局面。

    `setup` 的每一项是 [x, y, color]，**y=0 是底边** —— 与后端 snapshot、棋盘控件
    同一套坐标（这一条是本项目踩过的老坑，反过来就会把整道题上下翻转）。
    """
    size = int(problem.get("size") or 9)
    grid = [[EMPTY] * size for _ in range(size)]
    for item in (problem.get("setup") or []):
        x, y, color = int(item[0]), int(item[1]), int(item[2])
        if 0 <= x < size and 0 <= y < size:
            grid[y][x] = color
    return grid


def pct(value) -> str:
    """0~1 的比例 → 整百分比。用「加一半取整」而不是 round()：网页版是
    Math.round（half-up），Python 的 round 是 half-even，48.5% 两边会差 1% ——
    同一份数据两个界面差一个数字，查起来比写这行麻烦得多。"""
    if value is None:
        return "—"
    return f"{int(float(value) * 100 + 0.5)}%"


#: 服务端会下发 Markdown 记号的散文字段（其余是枚举文案、坐标与 id，不该动）。
#: 名字与 `api/tsumego.py`、`tsumego/puzzles.py` 里的字段名一致。
PROSE_KEYS = frozenset({"title", "hint", "note", "comment", "verdictText",
                        "source", "movesText", "family"})


def clean(obj):
    """把一包服务端数据里的散文字段过一遍 `plain()`（就地改，返回同一个对象）。

    在**收到数据时**洗一次，而不是在每个 `setText` 上各洗一遍：后者漏一处
    就是一个星号出现在屏幕上，而前者只要字段名对得上就漏不掉。
    理由与星号从哪来都写在 `ui/text.py` 的模块 docstring 里。
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str) and key in PROSE_KEYS:
                obj[key] = plain(value)
            else:
                clean(value)
    elif isinstance(obj, list):
        for value in obj:
            clean(value)
    return obj


class TsumegoPage(QWidget):
    """死活练习页。`refresh()` 由外壳在切页时调用（与大厅、对局页同一约定）。"""

    def __init__(self, api, sound: SoundPlayer | None = None, parent=None, prefs=None):
        super().__init__(parent)
        self._api = api
        self._sound = sound or SoundPlayer()
        #: 客户端偏好（自动下一题的开关存这儿）。不传也能跑：那就用默认值、不落盘。
        self._prefs = prefs
        # ---- 页面状态（与网页版的 useState 一一对应）
        self.items: list[dict] = []
        self.summary: dict | None = None
        self.current: dict | None = None
        self.board: list[list[int]] = [[EMPTY] * 9 for _ in range(9)]
        self.moves: list[list[int]] = []
        self.last_move: tuple[int, int] | None = None
        self.result: dict | None = None
        self.solution: dict | None = None
        self.show_hint = False
        self.busy = False
        self._first_run = True
        self._pending_moves: list[list[int]] = []
        #: 答对之后自动换题的定时器。**用户一动就取消**（重做/看答案/落子/点题库），
        #: 否则他正要看正解变化，题被换走了。
        self._auto_next = QTimer(self)
        self._auto_next.setSingleShot(True)
        self._auto_next.timeout.connect(self._ask_next)
        # 在途标记 + 脏标记：筛选连着改两次会并发两趟请求，回包顺序不保证，
        # 后到的旧结果会把新筛选的列表覆盖掉。做法是不并发第二趟 ——
        # 在途时只记一次脏，回完再补一趟。
        self._loading_list = False
        self._list_dirty = False
        #: 题库列表的行签名（第 32 轮）：一样就不重建，见 `_paint_list`
        self._list_sig = None
        self._build_ui()

    # ---------------------------------------------------------------- 组装

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(14)

        # ---------------- 左：错误条 + 横幅 + 棋盘
        left = QVBoxLayout()
        left.setSpacing(10)
        self.errorBar = Alert("err", "知道了", self)
        self.errorBar.button.clicked.connect(lambda: self.errorBar.show_text(""))
        left.addWidget(self.errorBar)
        left.addWidget(self._build_banner())

        self.boardView = GoBoard(size=9, interactive=True, parent=self)
        self.boardView.pointClicked.connect(self._on_point)
        left.addWidget(self.boardView, 1)

        self.emptyHint = QLabel("正在出题…", self)
        self.emptyHint.setProperty("role", "muted")
        self.emptyHint.setWordWrap(True)
        self.emptyHint.setAlignment(Qt.AlignCenter)
        left.addWidget(self.emptyHint)
        root.addLayout(left, 5)

        # ---------------- 右：侧栏（装进 QScrollArea，理由与对局页同：
        # 面板集合是随进度变的，四张卡同时出现时会超出可视高度，宁可侧栏滚，
        # 也要保证棋盘完整可见）
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        side = QWidget()
        side.setMinimumWidth(340)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(0, 0, 8, 0)
        sv.setSpacing(12)
        self.scroll.setWidget(side)
        root.addWidget(self.scroll, 3)

        sv.addWidget(self._build_problem_panel())
        self.solutionPanel = self._build_solution_panel()
        sv.addWidget(self.solutionPanel)
        sv.addWidget(self._build_progress_panel())
        sv.addWidget(self._build_library_panel())
        sv.addStretch(1)

    def _badge(self, text: str = "—", kind: str = "rank") -> QLabel:
        lab = QLabel(text, self)
        lab.setProperty("role", "badge")
        lab.setStyleSheet(theme.badge_style(kind))
        return lab

    def _build_banner(self) -> QWidget:
        """棋盘上方那道横幅。

        放左栏而不是侧栏：一行里要摆标题加三四个徽章，340px 的侧栏装不下 ——
        Qt 挤不动时不报错，只把文字两头各裁一截（对局页那个「手（pass」）。
        左栏有 ~660px，稳妥。分两行也是同一个理由。

        返回的是**控件**而不是布局：`left.addWidget(布局)` 会在建页时抛 TypeError。
        """
        box = QWidget(self)
        bv = QVBoxLayout(box)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(4)
        row1 = hbox(8)
        self.problemTitle = QLabel("死活练习", box)
        self.problemTitle.setProperty("role", "h2")
        self.kindBadge = self._badge("—")
        self.tierBadge = self._badge("—")
        row1.addWidget(self.problemTitle)
        row1.addWidget(self.kindBadge)
        row1.addWidget(self.tierBadge)
        row1.addStretch(1)
        row2 = hbox(8)
        self.goalBadge = self._badge("—", "")
        self.solvedBadge = self._badge("已解出", "ok")
        self.solvedBadge.setVisible(False)
        row2.addWidget(self.goalBadge)
        row2.addWidget(self.solvedBadge)
        row2.addStretch(1)
        bv.addLayout(row1)
        bv.addLayout(row2)
        return box

    def _build_problem_panel(self) -> QWidget:
        p = Panel("本题", self)
        self.taskLine = QLabel("", p)
        self.taskLine.setProperty("role", "muted")
        self.taskLine.setWordWrap(True)
        self.legendLine = QLabel("", p)
        self.legendLine.setProperty("role", "muted")
        self.legendLine.setWordWrap(True)
        p.body.addWidget(self.taskLine)
        p.body.addWidget(self.legendLine)

        # 判定结果：一条提示框 + 一行「本形结论」。提示框里放标题与讲解，
        # verdictText 单独一行灰字（网页版也是三级：状态标题 / comment / verdictText）。
        self.resultBar = Alert("info", "", p)
        self.verdictLine = QLabel("", p)
        self.verdictLine.setProperty("role", "muted")
        self.verdictLine.setWordWrap(True)
        p.body.addWidget(self.resultBar)
        p.body.addWidget(self.verdictLine)
        self.hintBar = Alert("warn", "", p)
        p.body.addWidget(self.hintBar)

        # 一行两个按钮：四个挤一行在 340px 侧栏里装不下（对局页就是这么裁掉字的）。
        r1 = hbox()
        self.btnNext = QPushButton("下一题", p)
        self.btnNext.setProperty("role", "primary")
        self.btnRedo = QPushButton("重做", p)
        r1.addWidget(self.btnNext)
        r1.addWidget(self.btnRedo)
        r1.addStretch(1)
        r2 = hbox()
        self.btnHint = QPushButton("提示", p)
        self.btnAnswer = QPushButton("看答案", p)
        r2.addWidget(self.btnHint)
        r2.addWidget(self.btnAnswer)
        r2.addStretch(1)
        p.body.addLayout(r1)
        p.body.addLayout(r2)

        # 自动下一题：默认开（连着做题才是练习的常态），关掉后停在正解上慢慢看。
        # 单独一行而不是塞进上面两行：340px 侧栏里那一行已经排满，再挤一颗控件
        # 就会把文字两头裁掉（对局页「虚手（pass）」的老教训）。
        r3 = hbox()
        self.chkAutoNext = QCheckBox("答对后自动下一题", p)
        self.chkAutoNext.setToolTip(
            f"答对后约 {AUTO_NEXT_MS / 1000:.1f} 秒自动换下一题；"
            "点「重做」「看答案」、再落一子或点题库里的一行都会取消这次自动换题。")
        self.chkAutoNext.setChecked(self._auto_next_enabled())
        self.chkAutoNext.toggled.connect(self._set_auto_next)
        r3.addWidget(self.chkAutoNext)
        r3.addStretch(1)
        p.body.addLayout(r3)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(2)
        # 值列吃掉剩余宽度：不给 stretch 的话，长文本会把整列撑到比侧栏还宽，
        # 而横向滚动条是关着的 —— 结果是文字尾部被默默切掉（无省略号）。
        grid.setColumnStretch(1, 1)
        self.problemRows: dict[str, QLabel] = {}
        for i, (key, cap) in enumerate((("attempts", "做题"), ("kind", "题型"),
                                        ("source", "出处"))):
            k = QLabel(cap, p)
            k.setProperty("role", "muted")
            # 「出处」整库同文（`library.SOURCE_NOTE` 是个模块级常量），它是版权说明
            # 而不是题目信息：开 wordWrap 要占 5 行，把「本题」卡撑到判定框跑出可视区。
            # 这一行只给一行，装不下就省略号，全文在 toolTip 里。
            v = ElidedLabel("—", p) if key == "source" else QLabel("—", p)
            if key != "source":
                v.setWordWrap(True)
            grid.addWidget(k, i, 0)
            grid.addWidget(v, i, 1)
            self.problemRows[key] = v
        p.body.addLayout(grid)

        self.btnNext.clicked.connect(self._ask_next)
        self.btnRedo.clicked.connect(self._redo)
        self.btnHint.clicked.connect(self._toggle_hint)
        self.btnAnswer.clicked.connect(self._reveal)
        return p

    def _build_solution_panel(self) -> QWidget:
        """答案与讲解。看过一次才建得出内容，所以整张卡默认收起。"""
        p = Panel("", self)
        head = hbox(8)
        t = QLabel("答案与讲解", p)
        t.setProperty("role", "h3")
        self.solutionBadge = self._badge("—", "")
        head.addWidget(t)
        head.addWidget(self.solutionBadge)
        head.addStretch(1)
        p.body.addLayout(head)
        self.solutionNote = QLabel("", p)
        self.solutionNote.setWordWrap(True)
        self.solutionNote.setProperty("role", "muted")
        p.body.addWidget(self.solutionNote)
        self.solutionRows = QVBoxLayout()
        self.solutionRows.setSpacing(8)
        p.body.addLayout(self.solutionRows)
        p.setVisible(False)
        return p

    def _build_progress_panel(self) -> QWidget:
        p = Panel("练习进度", self)
        self.progressGrid = QGridLayout()
        self.progressGrid.setContentsMargins(0, 0, 0, 0)
        self.progressGrid.setHorizontalSpacing(10)
        self.progressGrid.setVerticalSpacing(2)
        p.body.addLayout(self.progressGrid)
        return p

    def _build_library_panel(self) -> QWidget:
        p = Panel("", self)
        head = hbox(8)
        t = QLabel("题库", p)
        t.setProperty("role", "h3")
        self.countBadge = self._badge("0 题", "")
        head.addWidget(t)
        head.addWidget(self.countBadge)
        head.addStretch(1)
        p.body.addLayout(head)

        # 筛选条放在题库卡里、列表上方：它们改的就是这个列表（连题数徽章一起）。
        # 原先住在「练习进度」卡底部，看上去像在筛统计，而统计不受筛选影响。
        # kind + goal 一行，tier + 只没解出的一行；两个下拉平分一行宽：
        # 写死 120px 时「吃子手筋」四个字加箭头会被压字。
        f1 = hbox(8)
        self.cbKind = QComboBox(p)
        for value, label in KINDS:
            self.cbKind.addItem(label, value)
        self.cbGoal = QComboBox(p)
        f1.addWidget(self.cbKind)
        f1.addWidget(self.cbGoal)
        f2 = hbox(8)
        self.cbTier = QComboBox(p)
        self.cbTier.addItem("全部难度", "")
        for tier in TIERS:
            self.cbTier.addItem(tier, tier)
        self.chkUnsolved = QCheckBox("只做没解出的", p)
        self.chkWrong = QCheckBox("错题重练", p)
        self.chkWrong.setToolTip("只练做过但没解出的题；做对一道就从错题本里消失一道")
        f2.addWidget(self.cbTier)
        f2.addWidget(self.chkUnsolved)
        f2.addWidget(self.chkWrong)
        f2.addStretch(1)
        p.body.addLayout(f1)
        p.body.addLayout(f2)

        self.cbKind.currentIndexChanged.connect(self._on_kind_changed)
        self.cbGoal.currentIndexChanged.connect(self._on_goal_changed)
        self.cbTier.currentIndexChanged.connect(self._apply_filters)
        self.chkUnsolved.toggled.connect(self._apply_filters)
        self.chkWrong.toggled.connect(self._apply_filters)

        self.emptyList = QLabel("没有符合筛选条件的题目", p)
        self.emptyList.setProperty("role", "muted")
        self.emptyList.setWordWrap(True)
        self.emptyList.setVisible(False)
        p.body.addWidget(self.emptyList)
        self.problemList = QListWidget(p)
        # 固定一段高度：它的高度由内容决定时，一百多题会把整张卡撑成一堵墙，
        # 侧栏于是只能滚列表、别的面板都看不见。列表自己滚。
        self.problemList.setMinimumHeight(190)
        self.problemList.setMaximumHeight(300)
        self.problemList.setUniformItemSizes(True)
        self.problemList.itemClicked.connect(self._on_pick_row)
        p.body.addWidget(self.problemList)
        self.libraryPanel = p        # 测试要拿它断言「筛选条住在哪张卡里」
        return p

    # ---------------------------------------------------------------- 筛选

    def _kind(self) -> str:
        return str(self.cbKind.currentData() or "")

    def _goal(self) -> str:
        return str(self.cbGoal.currentData() or "")

    def _tier(self) -> str:
        return str(self.cbTier.currentData() or "")

    def _filter_query(self, with_unsolved: bool = True) -> dict:
        """三个筛选参数。空值不传 —— 后端对 `kind=` 会当成非法枚举报 422。"""
        query: dict = {}
        if self._kind():
            query["kind"] = self._kind()
        if self._goal():
            query["goal"] = self._goal()
        if self._tier():
            query["tier"] = self._tier()
        # `/next` 不认 unsolved（出题自己按「没做过 → 做错 → 复习」排），只有列表要
        if with_unsolved and self.chkUnsolved.isChecked():
            query["unsolved"] = True
        # 错题重练：列表与出题都认（后端 next 有 wrongOnly 参数）
        if self.chkWrong.isChecked():
            query["wrongOnly"] = True
        return query

    def _goal_options(self, kind: str) -> list[tuple[str, str]]:
        """某题型下**真的有题**的目标选项。

        静态表里的「劫活」是个死选项：眼位形家族里根本没有劫活形（守先都是净活，
        只有攻先才成劫，见后端枚举结论），选了只会筛出空列表、看着像题库坏了。
        用 summary.byGoal 把零题的目标剪掉 —— 将来题库补上了劫活题，选项自己回来，
        不必再改客户端。summary 还没加载时不剪，否则下拉会先闪成只剩「全部目标」。
        """
        base = list(GOALS_BY_KIND.get(kind) or GOALS_BY_KIND[""])
        by_goal = (self.summary or {}).get("byGoal") or {}
        if not by_goal:
            return base
        return [g for g in base
                if not g[0] or int((by_goal.get(g[0]) or {}).get("total") or 0) > 0]

    def _sync_goal_options(self) -> bool:
        """重摆目标下拉，返回**选中值有没有被动过**（动过就得重拉列表）。

        已选中的目标可能刚好被剪掉（例如停在「劫活」上刷新页面，summary 回来后才剪）：
        不重置的话下拉里根本没有当前值，控件显示成空白，而列表又确实是空的。
        """
        want = self._goal()
        options = self._goal_options(self._kind())
        keep = want if any(value == want for value, _label in options) else ""
        changed = keep != want
        self.cbGoal.blockSignals(True)        # 这次改选不是用户意图，不能反过来再拉一趟
        self.cbGoal.clear()
        for value, label in options:
            self.cbGoal.addItem(label, value)
        self.cbGoal.setCurrentIndex(next((i for i, (value, _l) in enumerate(options)
                                          if value == keep), 0))
        self.cbGoal.blockSignals(False)
        return changed

    def _on_kind_changed(self, *_a) -> None:
        # 换题型时旧目标可能不属于新题型（例如「吃子」+「做活」），
        # 也可能新题型下压根没有这个目标的题，留着就会筛出空列表
        self._sync_goal_options()
        self._load_problems()

    def _on_goal_changed(self, *_a) -> None:
        self._load_problems()

    def _apply_filters(self, *_a) -> None:
        self._load_problems()

    # ---------------------------------------------------------------- 数据

    def refresh(self) -> None:
        """切页进来时刷新。列表回包后再决定开哪道题（见 `_on_problems`）。"""
        self._load_problems()
        self._fetch_summary()

    def _load_problems(self) -> None:
        if self._loading_list:
            self._list_dirty = True
            return
        self._loading_list = True
        self._api.get("/api/tsumego/problems", self._filter_query()).finished.connect(
            self._on_problems)

    def _on_problems(self, data, err) -> None:
        self._loading_list = False
        if err is not None or not isinstance(data, dict):
            self.errorBar.show_text(f"读取题库失败：{err}", "err")
            # 请求在途时用户改过筛选 → 这一趟带的还是**旧条件**，而且它还失败了：
            # 必须把脏标记兑现成一次重试，否则列表与筛选勾选会一直自相矛盾，
            # 而且不会再自动拉一次（审计 L6）。只重试一次：脏标记在这一刻清掉。
            if self._list_dirty:
                self._list_dirty = False
                self._load_problems()
            return
        self.items = clean(data.get("items") or [])
        fresh = self._sync_current_from_list()
        self._paint_list()
        if fresh:
            # 只重画进度相关的两个区域：棋盘与判定文案已经由 `_on_attempt` 画对了，
            # 在这里再 `_paint()` 一次会把选中行的扰动叠上去。
            self._paint_head()
            self._paint_problem()
        if self._list_dirty:
            self._list_dirty = False
            self._load_problems()
            return
        if self._first_run:
            # 首次进入：由服务端出题（优先没做过的，其次做错的）。**必须排在列表之后**：
            # 两趟并发会各自开一题，谁后回谁赢，看着就是题目跳来跳去（网页版那条
            # 注释说的是同一件事，它用 await 串起来，这里用回包链起来）。
            self._first_run = False
            self._ask_next()
        elif self.current is None and self.items:
            # 改筛选后手上没题：退回列表第一题
            self._open(self.items[0])

    def _sync_current_from_list(self) -> bool:
        """把服务端最新的做题进度并回到手上这道题，返回**有没有变**。

        `current` 是开题那一刻的摘要，做完一题 / 看过一次答案之后它就在过期。
        网页版恰好漏了这一环：它重拉了列表与统计却没动 `current`，于是刚解出的题
        在题库列表里已经打了 ✓，横幅上那颗「已解出」徽章却不出现，「做题」那一行
        也停在旧次数 —— 同一屏两个说法。判定回包里就带着 solved / attempts，
        进度没道理等到下一题才更新。

        以列表里的那一行为准（它是全量重拉，`seenAnswer` 也在里面）；
        列表里没它时才退回用判定回包 —— 没它通常是刚被筛掉（例如勾着「只做没解出」
        把它解了），那一瞬间恰恰最需要徽章跟着变。
        """
        if not self.current:
            return False
        pid = self.current.get("id")
        keys = ("attempts", "solved", "seenAnswer")
        for row in self.items:
            if row.get("id") == pid:
                if any(row.get(k) != self.current.get(k) for k in keys):
                    self.current = row
                    return True
                return False
        if not self.result:
            return False
        patched = dict(self.current)
        patched["attempts"] = self.result.get("attempts", patched.get("attempts"))
        patched["solved"] = bool(self.result.get("solved"))
        if patched["solved"] != bool(self.current.get("solved")) \
                or patched["attempts"] != self.current.get("attempts"):
            self.current = patched
            return True
        return False

    def _fetch_summary(self) -> None:
        self._api.get("/api/tsumego/summary").finished.connect(self._on_summary)

    def _on_summary(self, data, err) -> None:
        if err is not None or not isinstance(data, dict):
            return
        self.summary = data
        if self._sync_goal_options():
            self._load_problems()
        self._paint_progress()

    def _ask_next(self) -> None:
        """下一题：只带三个筛选参数，不带 unsolved（后端不认，会 422）。"""
        self._cancel_auto_next()
        self._set_busy(True)
        self._api.get("/api/tsumego/next", self._filter_query(False)).finished.connect(
            self._on_next)

    # ---------------------------------------------------------------- 自动下一题

    def _auto_next_enabled(self) -> bool:
        return bool(self._prefs.auto_next) if self._prefs is not None else True

    def _set_auto_next(self, on: bool) -> None:
        if self._prefs is not None:
            self._prefs.auto_next = bool(on)
            self._prefs.sync()
        if not on:
            self._cancel_auto_next()

    def _cancel_auto_next(self) -> None:
        """用户一动手就取消这次自动换题（定时器停掉是幂等的）。"""
        if self._auto_next.isActive():
            self._auto_next.stop()

    def _arm_auto_next(self, status: str) -> None:
        """答对之后给下一题上闹钟。答错/继续应对不自动换 —— 那两种状态还要接着下。"""
        if status == SOLVED and self._auto_next_enabled():
            self._auto_next.start(AUTO_NEXT_MS)

    def _on_next(self, data, err) -> None:
        self._set_busy(False)
        if err is not None or not data or not (data.get("problem") or {}):
            if self.chkWrong.isChecked() and "错题本" in str(err or ""):
                self.errorBar.show_text("错题本里没有题目了——先把它们都做对再来。", "ok")
                return
            base = f"没能出题：{err}" if err else "服务端没有回题目"
            # 说清出路：出题失败后「下一题」就是重试按钮（审计 M3）
            self.errorBar.show_text(f"{base}，点「下一题」再试一次。", "err")
            return
        self._open(data["problem"])

    def _open(self, problem: dict) -> None:
        """开一道题：摆初始局面、清掉上一手的判定与答案。

        换题必须一次清干净，漏一个键就是上一题的残影（正解圈、幽灵子、答案面板）。
        """
        self._cancel_auto_next()      # 换题了，上一次的自动闹钟作废
        self.current = clean(problem)
        self.moves = []
        self._pending_moves = []
        self.last_move = None
        self.result = None
        self.solution = None
        self.show_hint = False
        size = int(problem.get("size") or 9)
        if self.boardView.board_size != size:
            self.boardView.set_size(size)
        self.board = setup_board(problem)
        self.errorBar.show_text("")
        self._paint()
        # 换题先把侧栏滚回顶部。题库列表在侧栏底部，点它一行会让 QScrollArea 跟着
        # 焦点把整条侧栏滚下去，刚开的题目与下一手的判定就跑到了可视区外。
        self.scroll.verticalScrollBar().setValue(0)

    def _redo(self) -> None:
        self._cancel_auto_next()
        if self.current:
            self._open(self.current)

    def _toggle_hint(self) -> None:
        self._cancel_auto_next()
        self.show_hint = not self.show_hint
        self._paint_problem()
        self._sync_busy()          # 按钮上的字要在「提示 / 收起提示」之间跟着翻

    def _reveal(self) -> None:
        if not self.current:
            return
        self._cancel_auto_next()   # 正要看答案，别让闹钟把题换走
        self._set_busy(True)
        self._api.get(f"/api/tsumego/{self.current['id']}/solution").finished.connect(
            self._on_solution)

    def _on_solution(self, data, err) -> None:
        self._set_busy(False)
        if err is not None or not isinstance(data, dict):
            self.errorBar.show_text(f"答案没取到：{err}", "err")
            return
        self.solution = clean(data)
        self.show_hint = True
        self._sound.play("click")
        # 看答案会记一次「看过答案」与一次做题次数（后端就是这么算的），所以列表要重拉
        self._load_problems()
        self._fetch_summary()
        self._paint_solution()
        self._paint_problem()
        self._sync_busy()

    def _on_pick_row(self, item) -> None:
        """点题库里的一行。表头行没有 UserRole，直接忽略。"""
        if item is None or self.busy:
            return
        pid = item.data(Qt.UserRole)
        if not pid:
            return
        self._sound.play("click")
        for row in self.items:
            if row.get("id") == pid:
                self._open(row)
                return

    def _on_point(self, x: int, y: int) -> None:
        if not self.current or self.busy or self.done:
            return
        if self.board[y][x] != EMPTY:
            self.errorBar.show_text("该点已有棋子", "err")
            return
        self._cancel_auto_next()      # 又落子 = 他还在这一题上
        self._set_busy(True)
        # 每次提交**整串自己的手顺**，不是最新一手：多手题要由服务端把已确认的
        # 对手应手一起回放，才能从初始局面复现到这个点（见 api/tsumego.py 的 attempt）。
        self._pending_moves = self.moves + [[x, y]]
        self._api.post(f"/api/tsumego/{self.current['id']}/attempt",
                       {"moves": self._pending_moves},
                       timeout=60).finished.connect(self._on_attempt)

    def _on_attempt(self, data, err) -> None:
        self._set_busy(False)
        if err is not None or not isinstance(data, dict):
            # 失败也放一声 wrong：网页版就是这么定的（请求异常与答错同一个反馈）。
            # 一声不响只会让人以为是自己那一点没点上。
            self._sound.play("wrong")
            self.errorBar.show_text(f"判定失败：{err}", "err")
            return
        if not self._pending_moves:
            return
        x, y = self._pending_moves[-1]
        self.board = data.get("board") or self.board
        self.moves = self._pending_moves
        self.last_move = (int(x), int(y))
        self.result = clean(data)
        status = data.get("status")
        self._sound.play({SOLVED: "correct", FAILED: "wrong"}.get(status, "stone"))
        self._load_problems()      # 做题次数与「已解出」都变了，列表与统计得跟着新
        self._fetch_summary()
        self._paint()
        self._reveal_verdict()
        self._arm_auto_next(status)

    def _reveal_verdict(self) -> None:
        """把判定框带回可视区。

        先 `activate()` 再定位：刚 setText 的那几行还没算进几何，直接拿旧位置去
        `ensureWidgetVisible` 会少算判定框自己长出来的那几十像素。
        不修这一条，第二手之后的「✓ 正解！」会落在侧栏可视区外 ——
        声音响了、棋盘上圈也画了，就是看不见结论。"""
        side = self.scroll.widget()
        if side is not None and side.layout() is not None:
            side.layout().activate()
        self.scroll.ensureWidgetVisible(self.resultBar, 0, 16)

    def _set_busy(self, on: bool) -> None:
        self.busy = bool(on)
        self._sync_busy()

    # ---------------------------------------------------------------- 叠加层

    @property
    def done(self) -> bool:
        """这一题已经判出结果（对或错），棋盘就不再接受落子。"""
        return bool(self.result) and self.result.get("status") in (SOLVED, FAILED)

    def _stone_at(self, x: int, y: int) -> int:
        size = len(self.board)
        if 0 <= y < size and 0 <= x < len(self.board[y]):
            return self.board[y][x]
        return EMPTY

    def _variation(self) -> list[dict]:
        """正解后的后续变化（幽灵子 + 序号）。

        只在**解出且有 pv** 时画，与网页版同口径：pv 是搜索给出的后续，没解出来就
        把它画上等于提前公布答案（答案本来就不该在解出之前下发）。
        颜色从「轮到走的对方」开始交替：pv 的第一手不是玩家下的。
        """
        if not self.current or not self.result:
            return []
        if self.result.get("status") != SOLVED:
            return []
        color = WHITE if int(self.current.get("toMove") or BLACK) == BLACK else BLACK
        out = []
        for point in (self.result.get("pv") or []):
            if int(point[0]) == -1:
                # 脱先哨兵（L3 连络题）：不画子，但著法权照常翻转
                color = WHITE if color == BLACK else BLACK
                continue
            out.append({"x": int(point[0]), "y": int(point[1]), "color": color})
            color = WHITE if color == BLACK else BLACK
        return out

    def _marks(self) -> list[dict]:
        """目标子、自己要救的那块，以及最后那一手的评价圈。"""
        if not self.current:
            return []
        out: list[dict] = []
        # 目标子常驻标记：吃子题要提掉的那几颗、对杀题对方的那块棋。
        # 不标出来学员根本不知道题目在问哪一块（尤其吃子题盘上白子不止一块）。
        # 已被提掉的不再画：在空点上画圈看着像个 bug。
        for point in (self.current.get("targets") or []):
            x, y = int(point[0]), int(point[1])
            if self._stone_at(x, y):
                out.append({"x": x, "y": y, "kind": "target"})
        for point in (self.current.get("own") or []):
            x, y = int(point[0]), int(point[1])
            if self._stone_at(x, y):
                out.append({"x": x, "y": y, "kind": "own"})
        if not self.result or not self.last_move:
            return out
        status = self.result.get("status")
        if status == SOLVED:
            out.append({"x": self.last_move[0], "y": self.last_move[1],
                        "kind": "good", "label": "正"})
        elif status == FAILED:
            out.append({"x": self.last_move[0], "y": self.last_move[1],
                        "kind": "blunder", "label": "错"})
            ref = self.result.get("refutation") or []
            if ref:
                # 「应」= 对方最好的那一手：让玩家看见自己为什么错
                out.append({"x": int(ref[0][0]), "y": int(ref[0][1]),
                            "kind": "bad", "label": "应"})
        # `continue`（多手题走对了一半）**什么都不标**：网页版在这一支会顺手把
        # 玩家刚下对的这一手标成红色「错」，与同一屏那句「… 对，继续应对」当场
        # 打架。这里不照抄 —— 与对局页那个「虚手」按钮同一处置：明知是坏的就不搬。
        return out

    # ---------------------------------------------------------------- 绘制

    def _paint(self) -> None:
        self._paint_head()
        self._paint_board()
        self._paint_problem()
        self._paint_solution()
        self._paint_progress()
        self._select_current_row()
        self._sync_busy()

    def _paint_head(self) -> None:
        cur = self.current
        self.emptyHint.setVisible(cur is None)
        if not cur:
            self.problemTitle.setText("死活练习")
            self.kindBadge.setText("—")
            self.tierBadge.setText("—")
            self.goalBadge.setText("—")
            self.solvedBadge.setVisible(False)
            return
        self.problemTitle.setText(cur.get("title") or "（无标题）")
        self.kindBadge.setText(cur.get("kindText") or "—")
        self.tierBadge.setText(f"{cur.get('tier', '—')} · 难度 {cur.get('difficulty', '—')}")
        self.goalBadge.setText(f"{cur.get('toMoveText', '')}先 · {cur.get('goalText', '')}"
                               f"（{cur.get('victimText', '')}棋）")
        self.solvedBadge.setVisible(bool(cur.get("solved")))

    def _paint_board(self) -> None:
        if not self.current:
            return
        self.boardView.set_props(board=self.board,
                                 last_move=({"x": self.last_move[0], "y": self.last_move[1]}
                                            if self.last_move else None),
                                 variation=self._variation(), marks=self._marks(),
                                 interactive=not self.done and not self.busy)

    def _paint_problem(self) -> None:
        cur = self.current
        if not cur:
            self.taskLine.setText("正在出题…")
            self.legendLine.setVisible(False)
            self.resultBar.show_text("")
            self.verdictLine.setVisible(False)
            self.hintBar.show_text("")
            for label in self.problemRows.values():
                label.setText("—")
            return
        task = GOAL_HINT.get(cur.get("goal") or "") or cur.get("goalText") or ""
        self.taskLine.setText(f"{cur.get('toMoveText', '')}先，目标：{task}。"
                              f"点击棋盘落子，判定由服务端完成。")
        targets = cur.get("targets") or []
        own = cur.get("own") or []
        legend = ""
        if targets:
            legend = f"蓝圈 = 目标子（{len(targets)} 颗）"
            if own:
                legend += "，紫圈 = 你这一块"
        self.legendLine.setText(legend)
        self.legendLine.setVisible(bool(legend))

        res = self.result
        if not res:
            self.resultBar.show_text("")
            self.verdictLine.setVisible(False)
        else:
            head = RESULT_HEAD.get(res.get("status"), "判定")
            kind = {SOLVED: "info", FAILED: "err"}.get(res.get("status"), "warn")
            fresh = not self.resultBar.label.text()    # 这一帧之前判定条是空的
            self.resultBar.show_text(f"{head}\n{res.get('comment', '')}", kind)
            if fresh:
                # 判定条是「这一手的结果」，出现时淡入一下 —— 它是页面上最该被看见的变化，
                # 原先它和别的文字一样是"啪"地一下出现，眼睛容易漏掉（第 32 轮动效）
                motion.fade_in(self.resultBar)
            verdict = res.get("verdictText") or ""
            self.verdictLine.setText(verdict)
            self.verdictLine.setVisible(bool(verdict))
        # 提示只在**还没判出结果**时给：判完再留着「怎么做才对」的提示是马后炮，
        # 也会让人误以为那句提示是刚刚的判定理由（网页版同口径 `!result && showHint`）。
        show_hint = bool(self.show_hint) and not res and bool(cur.get("hint"))
        self.hintBar.show_text(f"提示：{cur.get('hint')}" if show_hint else "", "warn")

        seen = " · 看过答案" if cur.get("seenAnswer") else ""
        self.problemRows["attempts"].setText(f"{cur.get('attempts', 0)} 次{seen}")
        self.problemRows["kind"].setText(f"{cur.get('kindText', '')} · "
                                        f"{cur.get('family', '')}")
        self.problemRows["source"].setText(cur.get("source") or "—")

    def _paint_solution(self) -> None:
        sol = self.solution
        self.solutionPanel.setVisible(bool(sol))
        if not sol:
            return
        self._drain(self.solutionRows)
        self.solutionBadge.setText(f"{sol.get('kindText', '')} · {sol.get('goalText', '')}")
        note = sol.get("note") or ""
        self.solutionNote.setText(note)
        self.solutionNote.setVisible(bool(note))
        for line in (sol.get("lines") or []):
            correct = line.get("result") == "correct"
            bar = Alert("info" if correct else "warn", "", self.solutionPanel)
            bar.show_text(f"{'正解' if correct else '失败图'}　"
                          f"{line.get('movesText', '')}", "info" if correct else "warn")
            self.solutionRows.addWidget(bar)
            comment = line.get("comment") or ""
            if comment:
                lab = QLabel(comment, self.solutionPanel)
                lab.setProperty("role", "muted")
                lab.setWordWrap(True)
                self.solutionRows.addWidget(lab)

    def _paint_progress(self) -> None:
        self._drain(self.progressGrid)
        rows: list[tuple[str, str]]
        # 拿不准口径的数字在标题上挂一句 toolTip，而不是把名字猜成一个好看的说法。
        # 网页版把 `solved / attempts` 叫「一次做对率」（TsumegoPage.tsx:493），
        # 可后端算的就是这个比值（api/tsumego.py 的 summary），与「一次」无关。
        # 名字改成与算法对得上的，口径不动 —— 它是唯一能拿到的数据。
        tips = {"答对率": "已解出题数 ÷ 做题次数。每道题都一次做对时是 100%；"
                        "反复做错、看过答案再做对，这个数会降下来。"}
        if not self.summary:
            rows = [("已解出", "加载中…")]
        else:
            s = self.summary
            rows = [("已解出", f"{s.get('solved', 0)} / {s.get('total', 0)} 题"),
                    ("做题次数", str(s.get("attempts", 0))),
                    ("答对率", pct(s.get("solveRate")))]
            for group in (s.get("byKind") or {}).values():
                rows.append((str(group.get("text", "")),
                             f"{group.get('solved', 0)} / {group.get('total', 0)}"))
            # 按难度档的进度：题型告诉你「练的是什么」，档位告诉你「练到多难了」。
            # 后端已按 TIERS 的固定顺序输出，这里不再排。
            for name, group in (s.get("byTier") or {}).items():
                rows.append((str(name),
                             f"{group.get('solved', 0)} / {group.get('total', 0)}"))
        for i, (cap, value) in enumerate(rows):
            tip = tips.get(cap, "")
            k = QLabel(cap)
            k.setProperty("role", "muted")
            k.setToolTip(tip)
            v = QLabel(value)
            v.setToolTip(tip)
            self.progressGrid.addWidget(k, i, 0)
            self.progressGrid.addWidget(v, i, 1)

    def _grouped(self) -> list[tuple[str, list[dict]]]:
        """先按题型分大组、组内按难度排：一百多题平铺成一个列表根本没法看。"""
        buckets: dict[str, list[dict]] = {}
        for item in self.items:
            buckets.setdefault(item.get("kindText") or "死活", []).append(item)
        out = sorted(buckets.items(), key=lambda kv: (
            KIND_ORDER.index(kv[0]) if kv[0] in KIND_ORDER else len(KIND_ORDER), kv[0]))
        # 同难度内的次序：网页版按拼音（localeCompare 'zh'），这里按 Unicode 码点。
        # 只差同一难度里的先后，不影响任何判定，本轮不为它引一套 locale 依赖
        # （Windows 上的 setlocale('zh_CN.UTF-8') 并不可靠）。
        return [(name, sorted(rows, key=lambda it: (
            int(it.get("difficulty") or 1), str(it.get("title") or ""))))
                for name, rows in out]

    def _paint_list(self) -> None:
        # 列表只在**内容真的变了**时重建（第 32 轮）。414 题重建一次实测 3.5 ms，
        # 而切回本页会重拉一遍同样的列表 —— 每次切页都白重建一张一模一样的表。
        # 签名里的每一项都是画进行里的东西：少一项，做题进度变了列表就不会更新。
        sig = (len(self.items),
               tuple((it.get("id"), it.get("title"), it.get("solved"), it.get("kindText"),
                      it.get("difficulty"), it.get("tier"), it.get("family"))
                     for it in self.items))
        self.countBadge.setText(f"{len(self.items)} 题")
        if sig == self._list_sig:
            self._select_current_row()
            return
        self._list_sig = sig
        self.problemList.clear()
        groups = self._grouped()
        self.emptyList.setVisible(not groups)
        for kind_name, items in groups:
            head = QListWidgetItem(f"{kind_name}　{len(items)} 题", self.problemList)
            head.setFlags(Qt.NoItemFlags)          # 表头不可选、不可点
            head.setForeground(theme.color(theme.MUTED))
            for item in items:
                mark = "✓ " if item.get("solved") else ""
                entry = QListWidgetItem(f"{mark}{item.get('title', '')}", self.problemList)
                entry.setData(Qt.UserRole, item.get("id", ""))
                entry.setToolTip(f"{item.get('title', '')}（{item.get('tier', '')} · 难度 "
                                 f"{item.get('difficulty', '')} · {item.get('family', '')}）")
                if item.get("solved"):
                    entry.setForeground(theme.color(theme.OK))
        self._select_current_row()

    def _select_current_row(self) -> None:
        """把当前题那一行选上（对应网页版那颗 `chip on`）。

        只在它真在列表里、而且当前选中行不是它时才动：`setCurrentRow` 会把那行
        滚进视野，每次落子都重摆一遍就会让题库列表突然跳回题目那一行 —— 很扰。
        筛选项把当前题筛掉时也不动（题还在手上，只是列表里没它）。
        """
        if not self.current:
            return
        pid = self.current.get("id")
        for i in range(self.problemList.count()):
            if self.problemList.item(i).data(Qt.UserRole) == pid:
                if self.problemList.currentRow() != i:
                    self.problemList.setCurrentRow(i)
                return

    def _sync_busy(self) -> None:
        has = bool(self.current)
        # 「下一题」是出题失败后**唯一的重试入口**（审计 M3）：没有当前题时也
        # 不能把它禁掉。从前 `has` 一起进判据，于是首次出题失败 = 四个按钮全灰、
        # 棋盘也不可点，界面上不留任何出路，只能切页再回来。
        self.btnNext.setEnabled(not self.busy)
        self.btnRedo.setEnabled(not self.busy and has and bool(self.moves))
        self.btnHint.setEnabled(not self.busy and has)
        self.btnAnswer.setEnabled(not self.busy and has and self.solution is None)
        self.btnHint.setText("收起提示" if self.show_hint else "提示")
        self.problemList.setEnabled(not self.busy)
        self.boardView.set_interactive(has and not self.done and not self.busy)

    @staticmethod
    def _drain(layout) -> None:
        """清空一个布局里的全部子控件与子布局（面板内容要整体重画时用）。"""
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout() is not None:
                TsumegoPage._drain(item.layout())
