"""对局页：棋盘 + 右侧形势/曲线位/操作/结算/终局面板。

对应 `frontend/src/pages/GamePage.tsx`（布局与文案）+ `store/game.ts`（状态机）。
两边逐条对齐，所以每处文案旁边都写着它抄自哪里 —— 后面改文案的人能找到出处。

刻意保留网页版四个"不显眼、改错了就出事"的口径：
  · **倒计时以服务端 `moveSecondsLeft` 为准，本地只走显示**：服务端只下发"事件发出
    那一刻还剩几秒"，本地按单调时钟往下数；不拿服务器时间戳做差是为了避开两端时钟偏差；
  · **终局点死子不重算预览**：`toggleDead` 只在本地增删列表，面板只更新"死子 N 枚"，
    真正的重算发生在 `scoreConfirm`（在服务端）；
  · **强制结束走 REST 而不是 WS**：它还得能对"内存里没有、只剩一条数据库记录"的卡死
    对局生效。WS 也连着时后端会 emit 同一条事件，这里再 apply 一次对**状态**是幂等的，
    对**音效**不幂等 —— 所以终局的副作用只在第一次落定时做，见 `_on_game_end`；
  · **`countsForRank` 是可选字段**（只有强制结束带 `false`）：缺键必须当成"计入战绩"，
    写成 `is True` 会把每一局都判成作废。

原生端独有的两件事：SGF 导出（网页版的 SGF 只存在记录里，没有下载口）与
终局后把段位进度回抛给外壳（网页版靠 zustand 的全局 store，原生端得走信号）。
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from core import ws as ws_mod
from core.api import err_text
from core.sound import SoundPlayer
from ui.widgets.board import GoBoard
from ui.widgets.parts import Alert, Panel, hbox
from ui.widgets import motion
from ui.widgets.winrate import WinrateChart

from .. import theme

EMPTY, BLACK, WHITE = 0, 1, 2

#: 思考中的动画字符。QSS 没有 .spinner，用文本转轮代替 —— 状态提示要"活着"，
#: 否则 0.4 秒的思考延迟会被读成"点没反应"。
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def color_name(color: int) -> str:
    return {BLACK: "黑", WHITE: "白"}.get(color, "—")


# ------------------------------------------------------------------ 局面重建
# 逐式移植 frontend/src/lib/board.ts：回看第 n 手时不能用服务端快照（那是当前局面），
# 必须按手顺重放。两处都要眼下的规则，所以只有一份实现，抄一份迟早会漂移。

def handicap_stones(size: int, handicap: int) -> list[tuple[int, int]]:
    """标准让子星位（与后端 rules.handicap_stones 同式）。"""
    if handicap < 2 or size % 2 == 0:
        return []
    c = size // 2
    e = c - 3 if size >= 13 else c - 2
    lo, hi = c - e, c + e
    corners = [(lo, lo), (hi, hi), (hi, lo), (lo, hi)]
    sides = [(lo, c), (hi, c), (c, lo), (c, hi)]
    table = {
        2: corners[:2],
        3: corners[:3],
        4: corners,
        5: [*corners, (c, c)],
        6: [*corners, sides[0], sides[1]],
        7: [*corners, sides[0], sides[1], (c, c)],
        8: [*corners, *sides],
        9: [*corners, *sides, (c, c)],
    }
    return table.get(handicap, [])


def board_at(size: int, moves: list[dict], upto: int, handicap: int) -> list[list[int]]:
    """前 `upto` 手之后的局面（upto=0 即开局）。"""
    b = [[EMPTY] * size for _ in range(size)]
    for x, y in handicap_stones(size, handicap):
        b[y][x] = BLACK
    for m in moves[:max(0, upto)]:
        x, y = m.get("x"), m.get("y")
        if x is None or y is None:
            continue
        b[y][x] = m.get("color")
        for cx, cy in (m.get("captures") or []):
            b[cy][cx] = EMPTY
    return b


def last_move_of(moves: list[dict], upto: int | None = None) -> dict | None:
    n = len(moves) if upto is None else upto
    for i in range(n - 1, -1, -1):
        m = moves[i] if 0 <= i < n else None
        if m and m.get("x") is not None and m.get("y") is not None:
            return {"x": m["x"], "y": m["y"]}
    return None


def build_curve(moves: list[dict], analyses: list[dict]) -> list[dict]:
    """胜率曲线的点。与 store/game.ts 的 buildCurve 同口径：
    没有 winrateBlack 的点（缺分析、让子首手未分析）不进曲线，散点却仍按 ply 对齐手数。"""
    out = []
    for i, a in enumerate(analyses):
        if a.get("winrateBlack") is None:
            continue
        out.append({
            "ply": i,
            "moveNum": i,
            "winrateBlack": a.get("winrateBlack"),
            "winrateWhite": a.get("winrateWhite"),
            "scoreLead": a.get("scoreLead"),
            "visits": a.get("visits"),
            "color": moves[i - 1].get("color") if 0 < i <= len(moves) else None,
        })
    return out


# ------------------------------------------------------------------ 对局页

class GamePage(QWidget):
    """一整局棋的界面。`open_game(game_id)` 是唯一入口。"""

    #: 终局后把最新段位进度回抛给外壳（对应网页版 useEffect 里的 setProgress）
    rankChanged = Signal(dict)
    backRequested = Signal()
    reviewRequested = Signal(str)
    #: 思考态翻转为真/假。做成信号而不是让外面轮询 `thinking`：两个 WS 事件挤在
    #: 同一次 `processEvents` 里到达时，瞬时值会被 `aiMove` 当场改回 False，
    #: 只看当前值就会把「确实亮过思考中」看成没亮（测试里拿到的是可重现结果）。
    thinkingChanged = Signal(bool)
    #: 属性默认值：`_reset_state()` 会给实例赋上，这里只防「读在写之前」。
    _thinking = False

    @property
    def thinking(self) -> bool:
        return self._thinking

    @thinking.setter
    def thinking(self, on: bool) -> None:
        on = bool(on)
        if on != self._thinking:
            self._thinking = on
            self.thinkingChanged.emit(on)

    def __init__(self, api, sound: SoundPlayer | None = None, parent=None):
        super().__init__(parent)
        self._api = api
        self._sound = sound or SoundPlayer()
        self._socket: ws_mod.GameSocket | None = None
        self.game_id = ""
        self._force_busy = False
        self._review_polls = 0
        self._sgf_target = ""
        self._spin = 0
        # 观战开关是「本会话的用户选择」，不是「本局状态」：换局不得重置。
        # （这两个键曾放在 `_reset_state` 里 → 上一局关掉「显示推荐点」，
        # 下一局又默认显示，而复选框还停在关 —— 用户现场报的
        # 「没开启却一直显示、再开再关一次才消失」就是这里。）
        self.show_hint = True
        self.show_ownership = True
        self._reset_state()

        self._build_ui()

        # 本地倒数的节拍：只在"服务端给了剩余秒数"时开着（见模块文档第一条）
        self._clock = QTimer(self)
        self._clock.setInterval(500)
        self._clock.timeout.connect(self._on_tick)
        # 思考动画的节拍
        self._think = QTimer(self)
        self._think.setInterval(120)
        self._think.timeout.connect(self._on_spin)
        # 终局后轮复盘进度（1.5 秒；失败 3 秒后重试，与网页版同参数）
        self._review = QTimer(self)
        self._review.setSingleShot(True)
        self._review.setInterval(1500)
        self._review.timeout.connect(self._poll_review)

        # 对局页可能「单独一面」打开（还没有任何对局）：第一帧就得画成
        # 「还没有对局」的空态（棋盘/侧栏藏起、提示语亮出），而不是一屏
        # 没初始化过的控件 —— 那是用户报过的逻辑 BUG 现场：白棋盘 + 一栏
        # 假按钮，看着像能玩其实什么都不能点。
        self._paint()

    # ---------------------------------------------------------------- 状态

    def _reset_state(self) -> None:
        """等价于 store 的 reset()。换局时必须整体清，漏一个键就是上一局的残影。"""
        self.size = 19
        self.komi = 7.5
        self.handicap = 0
        self.player_color = BLACK
        self.next_color = BLACK
        self.board: list[list[int]] = [[EMPTY] * 19 for _ in range(19)]
        self.moves: list[dict] = []
        self.analyses: list[dict] = []
        self.curve: list[dict] = []
        #: 上一次喂给曲线控件的数据指纹。`_paint` 会被「只改了回看哪一手」触发，
        #: 而曲线控件是全量重建 series 的 —— 没换数据就别重画（见 `_paint_chart`）。
        self._chart_sig: tuple | None = None
        self.meta: dict = {"rankName": "", "aiName": "", "aiTitle": "", "isPromotion": False,
                           "allowTakeback": True, "hintMode": True, "engine": ""}
        self.phase = "idle"
        self.status = "closed"
        self.thinking = False
        self.error = ""
        #: 一次性的成功提示（导出棋谱这类"做完了，但盘面上什么都不会变"的动作）。
        #: 与 `error` 共用最上面那条提示条，成功用 ok 色位（审计 S8）。
        self.notice = ""
        self.hint: list[dict] | None = None
        self.ownership: list[float] | None = None
        self.dead_stones: list[list[int]] = []
        self.despair_plies = 0
        self.move_seconds = 0
        self.move_seconds_left: int | None = None
        self.clock_stamp = 0.0
        self.scoring_preview: dict | None = None
        self.scoring_message = ""
        self.game_end: dict | None = None
        self.review_ready = False
        self.review: dict | None = None
        self.view_ply: int | None = None
        #: 本手在途锁（审计 S9）：`move` 已发出、服务端的落子/错误事件还没回来。
        #: 前端只能靠服务端回声推进局面，所以这段时间里再点一下 =
        #: 又发一手，服务端要么报「不是你走」要么下出两手 —— 双击就会连弹两次。
        self._move_busy = False

    # ---------------------------------------------------------------- 布局

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(14)

        # ---------------- 左：横幅 + 棋盘
        left = QVBoxLayout()
        left.setSpacing(10)
        self.errorBar = Alert("err", "知道了", self)
        self.errorBar.button.clicked.connect(self.clear_error)
        self.viewBar = Alert("info", "回到当前", self)
        self.viewBar.button.clicked.connect(lambda: self.set_view_ply(None))
        self.scoreBar = Alert("warn", "", self)
        left.addWidget(self.errorBar)
        left.addWidget(self.viewBar)
        left.addWidget(self.scoreBar)

        self.boardView = GoBoard(size=19, interactive=True, parent=self)
        self.boardView.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.boardView.pointClicked.connect(self._on_point)
        self.boardView.pointRightClicked.connect(self._on_right_point)
        left.addWidget(self.boardView, 1)

        self.emptyHint = QLabel("还没有对局。回大厅开一盘，或双击列表里那盘未下完的棋继续。", self)
        self.emptyHint.setProperty("role", "muted")
        self.emptyHint.setWordWrap(True)
        self.emptyHint.setAlignment(Qt.AlignCenter)
        self.emptyHint.setVisible(False)
        left.addWidget(self.emptyHint)
        root.addLayout(left, 5)

        # ---------------- 右：侧栏
        # 装进 QScrollArea：侧栏的面板集合是随阶段变的（对局操作 / 终局结算 / 终局面板），
        # 1280x800 下三张卡同时出现时会超出可视高度。网页版让整页滚，原生端只让侧栏滚，
        # 棋盘因此永远完整可见 —— 这是原生端比网页版强的地方，不能反过来牺牲掉。
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

        sv.addWidget(self._build_head())
        sv.addWidget(self._build_chart_box())
        sv.addWidget(self._build_ops())
        sv.addWidget(self._build_scoring())
        sv.addWidget(self._build_end())
        sv.addStretch(1)

    def _build_head(self) -> QWidget:
        p = Panel("", self)
        head = hbox()
        self.aiName = QLabel("AI", p)
        self.aiName.setProperty("role", "h3")
        self.rankBadge = QLabel("—", p)
        self.rankBadge.setProperty("role", "badge")
        self.rankBadge.setStyleSheet(theme.badge_style("rank"))
        self.promoBadge = QLabel("晋升战", p)
        self.promoBadge.setProperty("role", "badge")
        self.promoBadge.setStyleSheet(theme.badge_style("promo"))
        self.promoBadge.setVisible(False)
        self.connBadge = QLabel("未连接", p)
        self.connBadge.setProperty("role", "badge")
        head.addWidget(self.aiName)
        head.addWidget(self.rankBadge)
        head.addWidget(self.promoBadge)
        head.addStretch(1)
        head.addWidget(self.connBadge)
        p.body.addLayout(head)

        self.metaLine = QLabel("", p)
        self.metaLine.setProperty("role", "muted")
        self.metaLine.setWordWrap(True)
        p.body.addWidget(self.metaLine)

        gauge = hbox()
        self.gauge: dict[str, QLabel] = {}
        for key, cap in (("winrate", "我方胜率"), ("score", "目差（我 - 敌）"),
                         ("moves", "手数")):
            cell = QFrame(p)
            cell.setStyleSheet(f"QFrame {{ background: #f8f9fa; border: 1px solid {theme.LINE};"
                               f" border-radius: 10px; }}")
            cv = QVBoxLayout(cell)
            cv.setContentsMargins(8, 8, 8, 8)
            cv.setSpacing(0)
            v = QLabel("—", cell)
            v.setProperty("role", "stat")
            v.setAlignment(Qt.AlignCenter)
            v.setStyleSheet("background: transparent; border: none;")
            k = QLabel(cap, cell)
            k.setProperty("role", "muted")
            k.setAlignment(Qt.AlignCenter)
            k.setStyleSheet("background: transparent; border: none;")
            cv.addWidget(v)
            cv.addWidget(k)
            self.gauge[key] = v
            cell.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            gauge.addWidget(cell, 1)
        p.body.addLayout(gauge)

        self.stateLine = QLabel("", p)
        self.stateLine.setProperty("role", "muted")
        self.stateLine.setWordWrap(True)
        p.body.addWidget(self.stateLine)

        self.clockBar = Alert("info", "", p)
        self.thinkBar = Alert("info", "", p)
        self.despairBar = Alert("warn", "", p)
        p.body.addWidget(self.clockBar)
        p.body.addWidget(self.thinkBar)
        p.body.addWidget(self.despairBar)
        return p

    def _build_chart_box(self) -> QWidget:
        """胜率曲线。与网页版 GamePage 那张 `panel tight` 同构：控件 + 一句说明。"""
        p = Panel("", self)
        # 必须用 `p.body`，不能再 `QVBoxLayout(p)`：`Panel.__init__` 已经给这个 QFrame
        # 装过一个布局了，同一个控件装第二个布局 Qt 只留一句
        # `QLayout: Attempting to add QLayout ... which already has a layout` 就把它丢掉，
        # 于是占位标签根本没进布局 —— 它以默认的 100x30 漂在卡片左上角，
        # 文字被裁成「曲线在 P4 阶段接」（扫描断言 + 那行 stderr 一起抓出来的）。
        self.chartSlot = p.body
        #: 侧栏里只有 340px 宽，高度照网页版的 150 走（再高就把结算面板顶出屏）
        self.chart = WinrateChart(height=150, parent=p)
        self.chartSlot.addWidget(self.chart)
        # 原生端没有 hover tooltip，「点一下能回看」这件事必须说出来。
        # 而 150px 高算「紧凑档」（`winrate.COMPACT_BELOW`）：那里轴标题与图例
        # 都被收掉换回绘图区，所以颜色对应关系得由这一行接手 —— 不然
        # 「换了个画法把信息弄丢了」没人发现（网页版不需要说，它图例一直在）。
        self.chartHint = QLabel("蓝实线＝我方胜率，绿虚线＝目差；点击曲线任意位置可回看当时局面", p)
        self.chartHint.setProperty("role", "muted")
        self.chartHint.setAlignment(Qt.AlignCenter)
        self.chartHint.setWordWrap(True)
        self.chartSlot.addWidget(self.chartHint)
        # 点曲线跳手：与网页版 `onCurveSelect` 同口径 —— 点在最一手就是「退出回看」
        self.chart.pointClicked.connect(self._on_curve_click)
        return p

    def _on_curve_click(self, ply: int) -> None:
        live = len(self.moves)
        self.set_view_ply(None if ply >= live else ply)

    def _build_ops(self) -> QWidget:
        p = Panel("对局操作", self)
        self.opsPanel = p
        # 一行只放两个按钮。原先四个挤一行，在 340px 的侧栏里装不下：
        # Qt 布局挤不动时不报错也不警告，只把按钮压到 sizeHint 以下、
        # 文字**两头各裁一截**（开局截图上是「手（pass」）—— 不看图根本发现不了。
        r1 = hbox()
        r1b = hbox()
        self.btnPass = QPushButton("虚手（pass）", p)
        self.btnTakeback = QPushButton("悔棋两手", p)
        self.btnHint = QPushButton("给我提示", p)
        self.btnResign = QPushButton("投子认输", p)
        self.btnResign.setProperty("role", "danger")
        self.btnResignOk = QPushButton("确认认输", p)
        self.btnResignOk.setProperty("role", "danger")
        self.btnResignCancel = QPushButton("取消", p)
        for b in (self.btnResignOk, self.btnResignCancel):
            b.setVisible(False)
        for b in (self.btnPass, self.btnTakeback):
            r1.addWidget(b)
        for b in (self.btnHint, self.btnResign, self.btnResignOk, self.btnResignCancel):
            r1b.addWidget(b)
        r1.addStretch(1)
        r1b.addStretch(1)
        p.body.addLayout(r1)
        p.body.addLayout(r1b)

        r2 = hbox()
        self.btnForce = QPushButton("强制结束（不计胜负）", p)
        self.forceNote = QLabel("这一局作废，不影响战绩与晋升进度。", p)
        self.forceNote.setProperty("role", "muted")
        self.btnForceOk = QPushButton("确认强制结束", p)
        self.btnForceOk.setProperty("role", "danger")
        self.btnForceCancel = QPushButton("取消", p)
        for b in (self.forceNote, self.btnForceOk, self.btnForceCancel):
            b.setVisible(False)
        r2.addWidget(self.btnForce)
        r2.addWidget(self.forceNote)
        r2.addWidget(self.btnForceOk)
        r2.addWidget(self.btnForceCancel)
        r2.addStretch(1)
        p.body.addLayout(r2)

        r3 = hbox()
        # 网页版这两个也是 `.switch` 里的真 checkbox（GamePage.tsx:334/338）。
        # 原先用的是可勾选的 QPushButton，而 QSS 里 `QPushButton` 没有 `:checked`
        # 规则 —— 开与关像素级相同，点了看不出到底生效没有（大厅那个开关同一个病）。
        self.chkHint = QCheckBox("显示推荐点", p)
        self.chkOwnership = QCheckBox("显示领地热力", p)
        for b in (self.chkHint, self.chkOwnership):
            b.setChecked(True)
            r3.addWidget(b)
        self.noTakebackNote = QLabel("晋升战不可悔棋", p)
        self.noTakebackNote.setProperty("role", "muted")
        self.noTakebackNote.setVisible(False)
        r3.addWidget(self.noTakebackNote)
        r3.addStretch(1)
        p.body.addLayout(r3)

        # 推荐点**不能放一行**：侧栏只有 340px，而一枚「1. G3　52%」徽章就
        # 要 136px，三枚加“推荐：”一行根本装不下。hbox 不会换行，Qt 挤不动时
        # 不报错也不警告，只把三个都压到 sizeHint 以下 —— 文字两头各裁一截。
        # （这一条是中盘帧上的 `clipped_texts` 量出来的，不是看截图看出来的：
        # 徽章是分析回包到了才动态插进来的，开局那张图上一个都没有。）
        # 网页版靠 `.row { flex-wrap: wrap }` 自动换行，这里用两列网格等价。
        self.hintGrid = QGridLayout()
        self.hintGrid.setContentsMargins(0, 0, 0, 0)
        self.hintGrid.setHorizontalSpacing(8)
        self.hintGrid.setVerticalSpacing(4)
        self.hintCap = QLabel("推荐：", p)
        self.hintCap.setProperty("role", "muted")
        self.hintCap.setVisible(False)
        self.hintBadges: list[QLabel] = []
        self.hintGrid.addWidget(self.hintCap, 0, 0, 1, 2)
        self.hintGrid.setColumnStretch(2, 1)      # 右边留弹性，徽章不会被拉宽
        p.body.addLayout(self.hintGrid)

        r4 = hbox()
        self.btnSgf = QPushButton("导出 SGF 棋谱", p)
        r4.addWidget(self.btnSgf)
        r4.addStretch(1)
        p.body.addLayout(r4)

        # 虚手发 `{"action":"pass"}`，**不照抄网页版的 `{action:'move',x:null,pass:true}`**。
        # 后者服务端根本不认（`backend/app/api/ws.py` 的 move 分支见 x 为空就回
        # “缺少坐标”），即网页版那个「虚手」按钮现在是坏的 —— 它不会报错到让人
        # 发现的地步，只会弹一条“缺少坐标”，所以一直没人注意到。
        # 服务端已支持 pass 指令（point=None 即虚手），客户端按服务端的口径发。
        self.btnPass.clicked.connect(lambda: self.send({"action": "pass"}))
        self.btnTakeback.clicked.connect(lambda: self.send({"action": "takeback", "plies": 2}))
        self.btnHint.clicked.connect(lambda: self.send({"action": "hint"}))
        self.btnResign.clicked.connect(lambda: self._set_confirm_resign(True))
        self.btnResignOk.clicked.connect(self._do_resign)
        self.btnResignCancel.clicked.connect(lambda: self._set_confirm_resign(False))
        self.btnForce.clicked.connect(lambda: self._set_confirm_force(True))
        self.btnForceOk.clicked.connect(self._do_force_end)
        self.btnForceCancel.clicked.connect(lambda: self._set_confirm_force(False))
        self.chkHint.toggled.connect(self._set_show_hint)
        self.chkOwnership.toggled.connect(self._set_show_ownership)
        self.btnSgf.clicked.connect(self._on_export_sgf)
        return p

    def _build_scoring(self) -> QWidget:
        p = Panel("终局结算", self)
        self.scoringPanel = p
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        self.scoreRows: dict[str, QLabel] = {}
        for i, (key, cap) in enumerate((("black", "黑"), ("white", "白"),
                                        ("judge", "判定"), ("dead", "死子"))):
            k = QLabel(cap, p)
            k.setProperty("role", "muted")
            v = QLabel("—", p)
            v.setWordWrap(True)
            grid.addWidget(k, i, 0)
            grid.addWidget(v, i, 1)
            self.scoreRows[key] = v
        self.scoreGrid = grid
        p.body.addLayout(grid)
        r = hbox()
        self.btnScoreOk = QPushButton("确认终局", p)
        self.btnScoreOk.setProperty("role", "primary")
        self.btnResume = QPushButton("继续对局", p)
        r.addWidget(self.btnScoreOk)
        r.addWidget(self.btnResume)
        r.addStretch(1)
        p.body.addLayout(r)
        self.btnScoreOk.clicked.connect(
            lambda: self.send({"action": "scoreConfirm", "dead": self.dead_stones}))
        self.btnResume.clicked.connect(lambda: self.send({"action": "resume"}))
        p.setVisible(False)
        return p

    def _build_end(self) -> QWidget:
        p = Panel("", self)
        self.endPanel = p
        self.endTitle = QLabel("", p)
        self.endTitle.setProperty("role", "h3")
        self.endAlert = Alert("ok", "", p)
        self.endWords = QLabel("", p)
        self.endWords.setWordWrap(True)
        self.endAccuracy = QLabel("", p)
        self.endAccuracy.setProperty("role", "muted")
        self.rankEvents = QVBoxLayout()
        self.rankEvents.setSpacing(6)
        self.rankProgress = QLabel("", p)
        self.rankProgress.setProperty("role", "muted")
        self.rankProgress.setWordWrap(True)
        p.body.addWidget(self.endTitle)
        p.body.addWidget(self.endAlert)
        p.body.addWidget(self.endWords)
        p.body.addWidget(self.endAccuracy)
        p.body.addLayout(self.rankEvents)
        p.body.addWidget(self.rankProgress)

        r = hbox()
        self.btnReview = QPushButton("生成 AI 复盘", p)
        self.btnReview.setProperty("role", "primary")
        self.btnLobby = QPushButton("返回大厅", p)
        self.btnSgfEnd = QPushButton("导出 SGF 棋谱", p)
        r.addWidget(self.btnReview)
        r.addWidget(self.btnLobby)
        r.addWidget(self.btnSgfEnd)
        r.addStretch(1)
        p.body.addLayout(r)

        self.reviewBox = QFrame(p)
        rv = QVBoxLayout(self.reviewBox)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(4)
        self.reviewLabel = QLabel("", self.reviewBox)
        self.reviewLabel.setProperty("role", "muted")
        self.reviewBar = QProgressBar(self.reviewBox)
        self.reviewBar.setRange(0, 100)
        self.reviewBar.setValue(0)
        # 10px 的槽里画不下百分比（会被上下裁），网页版 `.progbar-track` 也是纯色槽、
        # 百分比在槽外的 `.progbar-text` 里。原生同口径：下面的 `reviewLabel` 带上百分数。
        # （QSS 里写 `text: none` 关不掉：Qt 不认这个属性，只会默默丢。）
        self.reviewBar.setTextVisible(False)
        rv.addWidget(self.reviewLabel)
        rv.addWidget(self.reviewBar)
        p.body.addWidget(self.reviewBox)
        self.reviewBox.setVisible(False)

        self.btnReview.clicked.connect(lambda: self.reviewRequested.emit(self.game_id))
        self.btnLobby.clicked.connect(self.backRequested.emit)
        self.btnSgfEnd.clicked.connect(self._on_export_sgf)
        p.setVisible(False)
        return p

    # ---------------------------------------------------------------- 对外接口

    def open_game(self, game_id: str) -> None:
        """进入一局棋：换连接、清状态。重复调用同一局是幂等的（不重连）。"""
        if not game_id:
            return
        if game_id == self.game_id and self._socket is not None:
            self.refresh()
            return
        self._teardown_socket()
        self._review.stop()
        self._reset_state()
        self.game_id = game_id
        self.emptyHint.setVisible(False)
        self.boardView.setVisible(True)
        self._sound.preload()
        self._socket = ws_mod.GameSocket(
            lambda: ws_mod.ws_url(self._api.base_url, f"/ws/game/{game_id}", self._api.token),
            parent=self)
        # 一律连绑定方法：连到临时闭包上会被静默丢投递（见 core/api.py 的 Reply 文档）
        self._socket.event.connect(self.apply_event)
        self._socket.statusChanged.connect(self._on_status)
        self._socket.connect_to_game()
        self._paint()
        self._fetch_title()

    def refresh(self) -> None:
        """外壳每次切页都会调。只做一件事：连接断了（含后端换了端口）就补一次。"""
        if self.game_id and self._socket is not None and self._socket.status == "closed":
            self._socket.reconnect()

    def on_base_url_changed(self) -> None:
        """后端自重启换了端口：当作一次全新的连接（重试预算也一起清零）。"""
        if self._socket is not None and self.game_id:
            self._socket.reconnect()

    def shutdown(self) -> None:
        """窗口关闭时断开长连接。留着它会让后端那条 WS 一直挂在 accept 里。"""
        # 轮询定时器也要停：`shutdown` 是在退出流程里调的，而计划 P5 立的口径是
        # 「退出时优雅关停」。复盘那一轮不拦，关窗瞬间还会再发一个请求出去。
        self._review.stop()
        # 两个本地 tick 同样要停（审计 S6）：上下钟每 500ms、思考转圈每 120ms
        # 都在刷 UI。页面是隐藏的，但它们由 QTimer 驱动，关窗后仍会心跳 ——
        # 退出流程里还在重绘，Windows 上表现为"关不干净"。
        self._clock.stop()
        self._think.stop()
        self._move_busy = False
        self._teardown_socket()

    def _teardown_socket(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket.deleteLater()
            self._socket = None

    # ---------------------------------------------------------------- 数据入口

    def _fetch_title(self) -> None:
        """补 `aiTitle`：WS 的 snapshot 里没有这一项（只有 rankName/aiName），
        而侧栏那行要写档位的人设描述。只取这一个键，别的都不覆盖 ——
        REST 详情里没有 board，多覆盖反而会把实时局面换成按手顺重放的近似值。"""
        if not self.game_id:
            return
        self._api.get(f"/api/games/{self.game_id}").finished.connect(self._on_title)

    def _on_title(self, data, err) -> None:
        if err is not None or not data:
            return
        g = data.get("game") or {}
        if g.get("id") and g["id"] != self.game_id:
            return                      # 慢回来的上一局响应，丢弃
        self.meta["aiTitle"] = g.get("aiTitle") or self.meta.get("aiTitle", "")
        self._paint_head()

    def send(self, msg: dict) -> None:
        if self._socket is None:
            self.error = "连接尚未建立，请稍候"
            self._move_busy = False       # 没发出去，在途锁也得解开（S9）
            self._paint()
            return
        self._socket.send(msg)

    def load_from_payload(self, g: dict) -> None:
        """等价于 store 的 loadFromPayload：拿一份全量 state 覆盖本地。"""
        size = g.get("size") or self.size
        moves = g.get("moves") or []
        board = g.get("board")
        if not (board and len(board) == size):
            board = board_at(size, moves, len(moves), g.get("handicap") or 0)
        self.size = size
        self.komi = g.get("komi", self.komi)
        self.handicap = g.get("handicap") or 0
        self.player_color = g.get("playerColor", self.player_color)
        self.next_color = g.get("nextColor", self.next_color)
        self.board = [list(row) for row in board]
        self.moves = list(moves)
        self.analyses = list(g.get("analyses") or [])
        curve = g.get("curve")
        self.curve = list(curve) if curve else build_curve(self.moves, self.analyses)
        self.meta = {
            "rankName": g.get("rankName", ""),
            "aiName": g.get("aiName", ""),
            "aiTitle": g.get("aiTitle") or self.meta.get("aiTitle") or "",
            "isPromotion": bool(g.get("isPromotion")),
            "allowTakeback": bool(g.get("allowTakeback", True)),
            "hintMode": bool(g.get("hintMode", True)),
            "engine": g.get("engine") or "",
            "profile": g.get("profile"),
        }
        self.phase = g.get("phase") or ("finished" if g.get("finished") else "playing")
        self.dead_stones = [list(p) for p in (g.get("scoringDead") or [])]
        self.despair_plies = g.get("despairPlies") or 0
        self.move_seconds = g.get("moveSeconds") or 0
        self.move_seconds_left = g.get("moveSecondsLeft")
        self.clock_stamp = time.monotonic()
        self.view_ply = None
        self._sync_clock_timer()
        if size != self.boardView.board_size:
            self.boardView.set_size(size)

    def apply_event(self, ev: dict) -> None:
        """状态机主干。分支与 store/game.ts 的 applyEvent 一一对应。

        **先验事件归属**（审计 S3）：socket 是 `deleteLater()` 异步拆的，旧局
        socket 上已经排队的信号仍可能投进新局的这个槽；带 id 的事件（`state`）与
        带 `gameId` 的事件（`reviewReady`）一律先对一下，不对就整条丢掉。
        """
        if not isinstance(ev, dict):
            return
        state = ev.get("state")
        gid = ev.get("gameId") or (state.get("id") if isinstance(state, dict) else None)
        if gid is not None and self.game_id and str(gid) != str(self.game_id):
            return
        kind = ev.get("type")
        if kind == "state":
            self.load_from_payload(ev.get("state") or {})
            self.thinking = False
        elif kind in ("move", "aiMove"):
            self._apply_move(ev, kind)
        elif kind == "analysis":
            self._apply_analysis(ev)
        elif kind == "hintOnly":
            self.hint = ev.get("hint") or self.hint
            if ev.get("ownership"):
                self.ownership = ev["ownership"]
        elif kind == "thinking":
            self.thinking = True
        elif kind == "hintMode":
            # 设置页改了「落子推荐」，服务端把它同步进进行中的对局并广播：
            # 推荐点要当场消失/出现，而不是等下一局（用户报过的 BUG 现场）。
            self.meta["hintMode"] = bool(ev.get("enabled", True))
        elif kind == "scoring":
            self._sound.play("click")
            self.phase = "scoring"
            self.dead_stones = [list(p) for p in (ev.get("deadStones") or [])]
            self.scoring_preview = ev.get("preview")
            self.scoring_message = ev.get("message") or ""
            self.thinking = False
            self.move_seconds_left = None
            self._sync_clock_timer()
        elif kind == "resume":
            self.load_from_payload(ev.get("state") or {})
            self.phase = "playing"
            self.scoring_preview = None
            self.dead_stones = []
        elif kind == "takeback":
            self._sound.play("click")
            self.load_from_payload(ev.get("state") or {})
            self.hint = None
            self.error = ""
        elif kind == "reviewReady":
            self._sound.play("review")
            self.review_ready = True
        elif kind == "gameEnd":
            self._move_busy = False
            self._on_game_end(ev)
            self._paint()
            return
        elif kind == "error":
            self._sound.play("wrong")
            self.error = ev.get("message") or "未知错误"
            self.thinking = False
        else:
            # 未知事件：不猜它是什么意思，但也不能装作没收到 ——
            # 协议漂移唯一的现场证据就是这里。
            self.error = f"收到未知事件：{kind}"
        # 服务端回了话（落子回声 / 报错 / 全量 state）就说明本手已经处理完，
        # 解开在途锁（审计 S9）。
        self._move_busy = False
        self._paint()

    def _apply_move(self, ev: dict, kind: str) -> None:
        m = ev.get("move") or {}
        grid = [list(row) for row in self.board]
        x, y, color = m.get("x"), m.get("y"), m.get("color")
        if x is not None and y is not None:
            grid[y][x] = color
            for cx, cy in (m.get("captures") or []):
                grid[cy][cx] = EMPTY
        # 提子声优先于落子声（同时发生只听一声）；黑白音色略不同便于分辨谁下的。
        # 虚手也播一声：网页版就是这样的（它不区分 m.x 是否为空），
        # 静默的 pass 反而会让玩家以为点丢了 —— 这里对齐蓝本不是偷懒。
        self._sound.play("capture" if m.get("captures")
                         else ("stone" if kind == "move" else "stoneAi"))
        self.board = grid
        self.moves.append(m)
        self.next_color = WHITE if color == BLACK else BLACK
        # 玩家落子后等 AI（thinking）；AI 落子后轮到玩家，提示等新分析带来
        self.thinking = kind == "move"
        self.hint = None
        # AI 落子后新一轮玩家限时才开始；玩家自己落子时不在计时
        self.move_seconds_left = ev.get("moveSecondsLeft") if kind == "aiMove" else None
        self.clock_stamp = time.monotonic()
        self._sync_clock_timer()

    def _apply_analysis(self, ev: dict) -> None:
        analyses = list(self.analyses)
        idx = int(ev.get("index") or 0)
        while len(analyses) < idx:
            analyses.append({"missing": True})
        if idx < len(analyses):
            analyses[idx] = ev.get("analysis") or {}
        else:
            analyses.append(ev.get("analysis") or {})
        self.analyses = analyses
        self.curve = build_curve(self.moves, analyses)
        a = ev.get("analysis") or {}
        if a.get("ownership"):
            self.ownership = a["ownership"]
        self.hint = ev.get("hint") or (a.get("candidates", [])[:3]
                                       if a.get("candidates") else self.hint)
        self.thinking = False
        if ev.get("moveSecondsLeft") is not None:
            # 后台分析比 aiMove 晚到一点，用它把倒计时校准回服务端口径
            self.move_seconds_left = ev["moveSecondsLeft"]
            self.clock_stamp = time.monotonic()
        self._sync_clock_timer()

    def _on_game_end(self, ev: dict) -> None:
        """落定终局。**同一条 gameEnd 会到两次**，所以有副作用的那半必须挡一下。

        强制结束走的是 REST，而服务端 `force_end()` 一边把同一个 dict 塞进响应、
        一边又 `live.emit(event)` 从 WS 推了一份（它不知道客户端有几条通道）。
        状态字段反复覆盖是幂等的，声音不是 —— 之前界面上就听到两声点击。
        判据用「本局此前有没有落定过」而不是比对两个 dict：服务端 `takeback`
        要求 `phase == "playing"`，一盘棋终局之后不会再有第二个真 gameEnd。
        """
        first = self.game_end is None
        self.phase = "finished"
        self.game_end = ev
        self.thinking = False
        self.hint = None
        self.move_seconds_left = None
        self._sync_clock_timer()
        if not first:
            return
        self._sound.play(self.end_sound(ev))
        progress = (ev.get("rank") or {}).get("progress")
        if progress:
            self.rankChanged.emit(progress)
        self._review_polls = 0
        self._review.stop()
        self._poll_review()          # 终局就查一次，之后 pending 才继续轮

    @staticmethod
    def end_sound(ev: dict) -> str:
        """终局音效：晋升/降级优先于胜负，其次是超时与 AI 投子（各有自己的声音）。"""
        rank = ev.get("rank") or {}
        if rank.get("promoted"):
            return "promote"
        if rank.get("demoted"):
            return "demote"
        if ev.get("reason") == "timeout":
            return "timeout"
        if ev.get("reason") == "ai-resign":
            return "resign"
        if ev.get("countsForRank", True) is False:
            return "click"           # 强制结束：不算胜负，不敲胜负音
        return "win" if ev.get("playerWon") else "lose"

    # ---------------------------------------------------------------- 交互

    @property
    def is_my_turn(self) -> bool:
        return (self.phase == "playing" and self.next_color == self.player_color
                and self.view_ply is None)

    def _on_point(self, x: int, y: int) -> None:
        if self.phase == "scoring":
            self.toggle_dead(x, y)
            return
        # `_move_busy`：本手已发出、服务端回声未到之前不再收第二手（审计 S9）。
        # `is_my_turn` 只看本地颜色，落子后要等服务端 `move` 事件才会翻转 ——
        # 这段窗口里双击会连发两手。
        if not self.is_my_turn or self._move_busy:
            return
        self._move_busy = True
        self.send({"action": "move", "x": x, "y": y})

    def _on_right_point(self, x: int, y: int) -> None:
        if self.phase == "scoring":
            self.toggle_dead(x, y)

    def toggle_dead(self, x: int, y: int) -> None:
        """点掉/恢复一枚死子。**纯本地**：预览不重算（那是服务端的事），
        面板只把"死子 N 枚"改一下 —— 与网页版同一口径，别顺手加"实时重算"。"""
        hit = next((i for i, (dx, dy) in enumerate(self.dead_stones)
                    if dx == x and dy == y), None)
        if hit is None:
            self.dead_stones.append([x, y])
        else:
            self.dead_stones.pop(hit)
        self._paint()

    def set_view_ply(self, ply: int | None) -> None:
        """回看第 ply 手后的局面。只改显示，不碰任何对局数据。"""
        if ply is not None and not 0 <= ply <= len(self.moves):
            ply = None
        self.view_ply = ply
        self._paint()

    def clear_error(self) -> None:
        self.error = ""
        self._paint()

    def _set_show_hint(self, value: bool) -> None:
        self.show_hint = bool(value)
        self._paint_board()

    def _set_show_ownership(self, value: bool) -> None:
        self.show_ownership = bool(value)
        self._paint_board()

    def _set_confirm_resign(self, on: bool) -> None:
        self.btnResign.setVisible(not on)
        self.btnResignOk.setVisible(on)
        self.btnResignCancel.setVisible(on)

    def _do_resign(self) -> None:
        self.send({"action": "resign"})
        self._set_confirm_resign(False)

    def _set_confirm_force(self, on: bool) -> None:
        self.btnForce.setVisible(not on)
        self.forceNote.setVisible(on)
        self.btnForceOk.setVisible(on)
        self.btnForceCancel.setVisible(on)

    def _do_force_end(self) -> None:
        """走 REST（见模块文档第三条）。响应里的 event 再 apply 一次，幂等。"""
        if self._force_busy or not self.game_id:
            return
        self._force_busy = True
        self.btnForceOk.setEnabled(False)
        self.btnForceOk.setText("结束中…")
        self._api.post(f"/api/games/{self.game_id}/force-end").finished.connect(self._on_forced)

    def _on_forced(self, data, err) -> None:
        self._force_busy = False
        self.btnForceOk.setEnabled(True)
        self.btnForceOk.setText("确认强制结束")
        self._set_confirm_force(False)
        if err is not None:
            self.apply_event({"type": "error", "message": err_text(err)})
            return
        event = (data or {}).get("event")
        if event:
            self.apply_event(event)

    # ---------------------------------------------------------------- SGF

    def _on_export_sgf(self) -> None:
        name = f"game_{self.game_id[:8]}.sgf" if self.game_id else "game.sgf"
        path, _selected = QFileDialog.getSaveFileName(self, "导出 SGF 棋谱", name,
                                                      "SGF 棋谱 (*.sgf);;所有文件 (*)")
        if path:
            self.export_sgf_to(path)

    def export_sgf_to(self, path: str) -> bool:
        """把本局棋谱写到 `path`。取服务端的 `/api/games/{id}/sgf`：
        它已经会处理"未终局也能导出"（按记录重建），客户端不必再实现一遍 SGF 生成。"""
        if not self.game_id:
            return False
        self._sgf_target = str(path)
        self._api.get_text(f"/api/games/{self.game_id}/sgf").finished.connect(self._on_sgf)
        return True

    def _on_sgf(self, data, err) -> None:
        target, self._sgf_target = self._sgf_target, ""
        if not target:
            return
        if err is not None or not isinstance(data, str) or not data.startswith("(;"):
            self.error = f"导出 SGF 失败：{data if err is None else err}"
            self._paint()
            return
        from pathlib import Path
        try:
            Path(target).write_text(data, encoding="utf-8")
        except OSError as exc:
            self.error = f"写入 {target} 失败：{exc}"
        else:
            # 成功也要说一声（审计 S8）：SGF 导出不改变盘面，从前"点完没反应"
            # 与"导出成功了"看起来一模一样。
            self.error = ""
            self.notice = f"棋谱已导出到 {target}"
        self._paint()

    # ---------------------------------------------------------------- 复盘轮询

    def _poll_review(self) -> None:
        if not self.game_id:
            return
        self._review_polls += 1
        self._api.get(f"/api/reviews/{self.game_id}/status").finished.connect(self._on_review)

    def _on_review(self, data, err) -> None:
        if err is not None or not data:
            if self.game_end and self._review_polls < 200:
                self._review.start(3000)
            return
        gid = data.get("gameId")
        if gid is not None and self.game_id and str(gid) != str(self.game_id):
            return                       # 旧局的复盘回包，别画进新局（审计 S3）
        self.review = data
        status = data.get("status")
        if status == "pending" and self.game_end and not self.review_ready:
            self._review.start(1500)          # 只有没下完的复盘才继续轮
        self._paint()

    # ---------------------------------------------------------------- 计时

    def _sync_clock_timer(self) -> None:
        if self.move_seconds_left is None:
            self._clock.stop()
        elif not self._clock.isActive():
            self._clock.start()
        if self.thinking and self.phase == "playing":
            if not self._think.isActive():
                self._think.start()
        else:
            self._think.stop()

    def _on_tick(self) -> None:
        # 服务端只给"当时还剩几秒"，之后按本地单调时钟往下走；
        # 用 monotonic 而不是系统时间：改系统时间/休眠唤醒都不该让倒计时跳变。
        # 超时不在这儿判：判负是服务端的事（它会发 gameEnd），本地走到 0 就停住等它。
        if self.move_seconds_left is None:
            self._clock.stop()
            return
        self._paint_clock()

    def _on_spin(self) -> None:
        self._spin = (self._spin + 1) % len(SPINNER)
        if self.thinking and self.phase == "playing":
            self.thinkBar.show_text(f"{SPINNER[self._spin]} "
                                    f"{self.meta.get('aiName') or 'AI'} 正在思考…")
        else:
            self._think.stop()

    def seconds_left(self) -> int:
        base = int(self.move_seconds_left or 0)
        return max(0, base - round(time.monotonic() - self.clock_stamp))

    # ---------------------------------------------------------------- 绘制

    def _paint(self) -> None:
        has_game = bool(self.game_id)
        self.emptyHint.setVisible(not has_game)
        self.boardView.setVisible(has_game)
        self.scroll.setVisible(has_game)
        if not has_game:
            return
        self.errorBar.show_text(self.error, "err")
        self.viewBar.show_text(
            f"正在查看第 {self.view_ply} 手后的局面（不影响对局）" if self.view_ply is not None
            else "", "info")
        self.scoreBar.show_text(
            (self.scoring_message or "终局结算：点击棋盘上的棋子可修正死子判定。")
            if self.phase == "scoring" else "", "warn")
        self._paint_head()
        self._paint_ops()
        self._paint_scoring()
        self._paint_end()
        self._paint_board()
        self._paint_chart()

    def _paint_chart(self) -> None:
        """把 `self.curve` 交给曲线控件，并把「正在看第几手」同步过去。

        重画只在数据真的变了时做（指纹 = 点数 + 最后一点的胜率与目差）：
        `set_data` 是拆掉全部 series 重建的，而拖动回看会高频走 `_paint`，
        每拖一下都重建一次图表既浪费又会让图「闪」。新分析到达时点数一定变，
        所以该重画的场景一个都没漏。
        """
        self.chart.setVisible(bool(self.curve))
        ply = len(self.moves) if self.view_ply is None else self.view_ply
        self.chart.set_current(ply)
        last = self.curve[-1] if self.curve else {}
        sig = (len(self.curve), last.get("winrateBlack"), last.get("scoreLead"),
               self.player_color)
        if sig == self._chart_sig:
            return
        self._chart_sig = sig
        self.chart.set_data(self.curve, (), self.player_color)

    def _paint_head(self) -> None:
        meta = self.meta
        self.aiName.setText(meta.get("aiName") or "AI")
        self.rankBadge.setText(meta.get("rankName") or "—")
        self.promoBadge.setVisible(bool(meta.get("isPromotion")))
        connected = self.status == "open"
        connecting = self.status in ("connecting", "reconnecting")
        self.connBadge.setText("已连接" if connected else ("连接中…" if connecting else "未连接"))
        self.connBadge.setStyleSheet(theme.badge_style("ok" if connected
                                                      else ("warn" if connecting else "muted")))
        # 这一行只留人设描述（如「陪你从入门走到冲段」）。早先还拼了
        # 「引擎 内置启发式引擎 · 96 visits」—— 那是给开发者看的技术参数，
        # 玩家读不懂也做不了任何事（第 33 轮清除开发者痕迹）。
        bits = [meta.get("aiTitle") or ""]
        self.metaLine.setText("　·　".join(b for b in bits if b))

        ply = len(self.moves) if self.view_ply is None else self.view_ply
        current = self.analyses[ply] if 0 <= ply < len(self.analyses) else {}
        wr = (current.get("winrateBlack") if self.player_color == BLACK
              else current.get("winrateWhite"))
        lead = current.get("scoreLead")
        self.gauge["winrate"].setText("—" if wr is None else f"{wr * 100:.1f}%")
        if lead is None:
            self.gauge["score"].setText("—")
        else:
            mine = lead if self.player_color == BLACK else -lead
            self.gauge["score"].setText(f"{mine:.1f}" if mine <= 0 else f"+{mine:.1f}")
        self.gauge["moves"].setText(str(len(self.moves)))

        turn = "轮到你落子" if self.is_my_turn else "AI 思考中"
        phase_words = {"playing": turn, "scoring": "终局结算", "finished": "已结束"}
        line = (f"我方执 {color_name(self.player_color)} · {self.size} 路　"
                f"贴目 {self.komi} · {phase_words.get(self.phase, '等待连接')}")
        if self.handicap:
            line += f" · 让 {self.handicap} 子"
        if self.move_seconds > 0:
            line += f" · 每手限时 {self.move_seconds} 秒"
        self.stateLine.setText(line)
        self.despairBar.show_text(
            f"对手形势绝望（{self.despair_plies} 手），可能很快投子认输"
            if self.despair_plies and self.phase == "playing" else "", "warn")
        self.thinkBar.show_text(
            f"{SPINNER[self._spin]} {self.meta.get('aiName') or 'AI'} 正在思考…"
            if (self.thinking and self.phase == "playing") else "")
        self._paint_clock()

    def _paint_clock(self) -> None:
        show = self.move_seconds_left is not None and self.is_my_turn
        if not show:
            self.clockBar.show_text("")
            return
        left = self.seconds_left()
        self.clockBar.show_text(
            "本手已超时，正在判定…" if left == 0 else f"本手剩余 {left} 秒，超时判负",
            "err" if left <= 10 else "info")

    def _paint_ops(self) -> None:
        playing = self.phase in ("playing", "scoring")
        self.opsPanel.setVisible(self.phase != "finished" and bool(self.game_id))
        mine = self.is_my_turn
        self.btnPass.setEnabled(mine)
        self.btnTakeback.setEnabled(self.phase == "playing"
                                    and self.meta.get("allowTakeback", True)
                                    and bool(self.moves) and self.view_ply is None)
        self.btnHint.setEnabled(self.phase == "playing")
        self.noTakebackNote.setVisible(not self.meta.get("allowTakeback", True))
        self.chkHint.setEnabled(bool(self.meta.get("hintMode")))
        self.btnForceOk.setEnabled(not self._force_busy)
        if not playing:
            self._set_confirm_resign(False)
            self._set_confirm_force(False)
        self.hintCap.setVisible(bool(self.show_hint and self.meta.get("hintMode")
                                      and self.hint and playing))
        for b in self.hintBadges:
            self.hintGrid.removeWidget(b)
            b.setParent(None)
            b.deleteLater()
        self.hintBadges = []
        if self.show_hint and self.meta.get("hintMode") and self.hint and playing:
            for i, c in enumerate(self.hint[:3]):
                txt = (f"{i + 1}. {c.get('gtp', '')}　"
                       f"{float(c.get('winrate') or 0) * 100:.0f}%")
                badge = QLabel(txt, self.opsPanel)
                badge.setProperty("role", "badge")
                badge.setStyleSheet(theme.badge_style(""))
                # 两列一行放三枚：第一行两枚（8+136+136=280 < 侧栏可用宽），第三枚落第二行
                self.hintGrid.addWidget(badge, 1 + i // 2, i % 2)
                self.hintBadges.append(badge)

    def _paint_scoring(self) -> None:
        self.scoringPanel.setVisible(self.phase == "scoring")
        if self.phase != "scoring":
            return
        pv = self.scoring_preview or {}
        self.scoreRows["black"].setText(
            f"{pv.get('blackStones', 0)} 子 + {pv.get('blackTerritory', 0)} 目"
            f" = {pv.get('blackTotal', 0)}")
        self.scoreRows["white"].setText(
            f"{pv.get('whiteStones', 0)} 子 + {pv.get('whiteTerritory', 0)} 目"
            f" + 贴目 {pv.get('komi', self.komi)} = {pv.get('whiteTotal', 0)}")
        self.scoreRows["judge"].setText(
            f"{pv.get('result', '—')}（{'数子' if pv.get('method') == 'area' else '数目'}）")
        self.scoreRows["dead"].setText(f"{len(self.dead_stones)} 枚（点击棋盘上的棋子可增减）")

    def _paint_end(self) -> None:
        end = self.game_end
        was = self.endPanel.isVisible()
        self.endPanel.setVisible(bool(end))
        if end and not was:
            # 终局面板是整局的高潮，淡入一下（第 32 轮动效）：它是从无到有出现的，
            # 原先"啪"地一下铺满右栏，注意力还在棋盘上的人会整块漏掉。
            motion.fade_in(self.endPanel)
        self.reviewBox.setVisible(bool(end) and (self.review or {}).get("status") in
                                  ("pending", "failed"))
        if not end:
            return
        self.endTitle.setText("你赢了！" if end.get("playerWon")
                              else ("对局已作废" if end.get("reason") == "force-end"
                                    else "本局结束"))
        force = end.get("reason") == "force-end"
        self.endAlert.show_text(end.get("resultText") or "", "warn" if force else "ok")
        self.endWords.setText(end.get("aiWords") or "")
        acc = end.get("avgLossPoints")
        self.endAccuracy.setText(
            f"吻合度：平均每手损失 {acc} 目" if acc is not None else "")
        for i in reversed(range(self.rankEvents.count())):
            item = self.rankEvents.takeAt(i)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for e in (end.get("rank") or {}).get("events") or []:
            kind = ("ok" if e.get("kind") == "promote"
                    else "err" if e.get("kind") in ("promo_fail", "demote") else "info")
            bar = Alert(kind, "", self.endPanel)
            bar.label.setText(e.get("detail") or "")
            self.rankEvents.addWidget(bar)
        pr = (end.get("rank") or {}).get("progress") or {}
        if pr:
            text = (f"当前 {pr.get('rankName', '')}　·　本级胜场 "
                    f"{pr.get('rankWins', 0)}/{pr.get('winsRequired', 0)}")
            if pr.get("inPromotion"):
                text += (f"　·　晋升战 {pr.get('promotionWins', 0)}"
                         f"/{pr.get('promotionRequired', 0)}")
            self.rankProgress.setText(text)
        self.btnReview.setText("查看 AI 复盘" if self.review_ready else "生成 AI 复盘")
        review = self.review or {}
        if review.get("status") == "pending":
            pct = int(max(0.0, min(1.0, float(review.get("progress") or 0))) * 100)
            self.reviewLabel.setText(
                f"{review.get('stageText') or '复盘中'}　{review.get('detail') or ''}　{pct}%")
            # 进度条走格而不是跳格：它每 1.2 秒被轮询更新一次，跳着涨看着像掉帧
            # （动效关掉时 `animate_value` 就是直接赋值，测试断言见 `motion.duration`）
            motion.animate_value(self.reviewBar, "value", pct, "bar")
        elif review.get("status") == "failed":
            self.reviewLabel.setText(
                f"复盘生成失败：{review.get('error') or '未知错误'}")

    def _paint_board(self) -> None:
        ply = len(self.moves) if self.view_ply is None else self.view_ply
        board = self.board if self.view_ply is None else board_at(
            self.size, self.moves, self.view_ply, self.handicap)
        show_hints = bool(self.is_my_turn and self.show_hint and self.meta.get("hintMode"))
        current = self.analyses[ply] if 0 <= ply < len(self.analyses) else {}
        # 回看时只用那一手自己的 ownership（没有就什么都不叠）：
        # 拿现在的地盘图去套旧局面会指鹿为马
        own = self.ownership if self.view_ply is None else current.get("ownership")
        self.boardView.set_props(
            board=board,
            last_move=last_move_of(self.moves, None if self.view_ply is None else self.view_ply),
            hints=(self.hint or []) if show_hints else [],
            show_hints=show_hints,
            ownership=own,
            show_ownership=self.show_ownership,
            # 终局之后**仍然留着**死子的叉：结论文字说的是「黑胜 X 目」，可那些被判死的子
            # 一旦不再画叉，盘上看着就是一局「白子还活着」的棋，与结论对不上。
            # 网页版是 `phase === 'scoring' ? deadStones : NO_DEAD`（终局即清空），
            # 这一条是原生端**有意偏离**：认输/超时局从没进过结算，dead_stones 本来就是空的。
            # 回看旧局面时不叠：拿终局的死子判定去套中盘的形状会指鹿为马（同上面的 own）。
            dead=([tuple(p) for p in self.dead_stones]
                  if self.view_ply is None and self.phase in ("scoring", "finished") else []),
            # 「可点」与「画不画死子叉」必须是同一个判据（审计 S1）：从前
            # `phase == "scoring"` 单独放行，于是回看旧局面时盘上既不画死子叉、
            # 却照样能点 —— 点下去提交的是旧局面的坐标，被当成死子判定发给服务端。
            interactive=((self.phase == "scoring" and self.view_ply is None)
                         or self.is_my_turn),
            dim=self.view_ply is not None,
        )

    # ---------------------------------------------------------------- 事件回调

    def _on_status(self, status: str) -> None:
        self.status = status
        self._paint()
