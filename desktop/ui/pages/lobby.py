"""大厅：生涯统计 + 晋升进度 + 引擎状态 + 开新局 + 对局列表。

对应 frontend/src/pages/LobbyPage.tsx。数据源与网页版完全一致，四个 GET：
  /api/auth/me、/api/games?limit=30、/api/system/status、/api/games/active

写操作三件：开新局（它是进对局页的唯一入口，不做开局的 P2 就进不去）、
删除选中记录、清空历史。强制结束放在对局页（那里才看得见当前局面）。

后两件第 20 轮才补上：P1 时这里写的是「删除/清历史本轮仍不做」，而那句话**没进
14.9 的欠账清单** —— 写在代码 docstring 里的「不做」等于没记，十九轮没人再捡起它，
直到用户自己发现「对局记录不可删」。两个端点后端一直都有（`games.py:267/292`），
且**只删明细不动聚合**：本级胜场驱动着晋升，删一盘棋就把战绩回退对学员是灾难。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QProgressBar, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from core.api import err_text
from ui.widgets import motion
from ui.widgets.parts import Alert, hbox

from .. import theme

# 引擎四态：就绪 / 预热中 / 断链自愈中 / 未安装。文案与网页版同一口径 ——
# 把"没装"说成"请重装"、或把一个只需等几秒的自愈说成"引擎坏了"，都是真实的用户伤害。
ENGINE_KIND = {"ready": "ok", "warming": "warn", "recovering": "warn", "missing": "muted"}

# 二次确认那一颗按钮上的字（也是从确认态返回原样时要复位成的字）。
# 文案口径抄网页版 `LobbyPage.tsx:350~366`，连「保留战绩」这四个字一起抄：
# 用户看到「清空记录」时手里悬着的那只手需要知道战绩不会没。
DEL_OK = "确认删除"
CLEAR_OK = "确认清空"
CONFIRM_HINT = {
    "del": "只删这一盘的明细，胜负场数与总胜率保留。",
    "clear": "删掉全部已结束的记录，未下完的那盘会跳过；战绩保留。",
}


def stats_note(data: dict) -> str:
    """把后端回传的「战绩还在」折成一句人话（口径抓网页版 `LobbyPage.tsx:58`）。

    为什么非要把数字念出来：用户删的是明细，心里怕的是「我的胜场与胜率没了」。
    只回一句「已删除」不足以让人放心，得把胜/负/总胜率原样报回去。
    """
    st = data.get("stats") or {}
    wins = int(st.get("totalWins") or 0)
    losses = int(st.get("totalLosses") or 0)
    winrate = float(st.get("winrate") or 0.0) * 100
    text = (f"已删除 {int(data.get('deleted') or 0)} 条记录，还剩 "
            f"{int(data.get('remaining') or 0)} 条。战绩已保留："
            f"{wins} 胜 {losses} 负，总胜率 {winrate:.1f}%")
    if data.get("skipped"):
        # 清空会跳过未结束的那几盘（批量删掉一盘正在下的棋太意外），必须说
        text += f"（跳过 {int(data['skipped'])} 盘未结束的对局）"
    return text


def engine_state(status: dict | None) -> tuple[str, str]:
    """把 /api/system/status 的 engine 段折成 (状态键, 中文说明)。"""
    if not status:
        return "muted", "引擎状态未知"
    eng = status.get("engine") or {}
    katago = eng.get("katago") or {}
    recover = eng.get("recover") or {}
    if eng.get("active") == "katago":
        return "ready", f"KataGo 就绪（{katago.get('binary', '') and '本地' or ''}分析引擎）"
    if eng.get("warming"):
        return "warming", "KataGo 预热中，稍候自动切换"
    if recover.get("watching"):
        # 两个数字都不能直接印：`attempt` 是「第几次重试**已经在跑**」，而看门狗先睡
        # 5 秒才把它置 1，所以每次断链都有 5 秒真的报 0（P5 验收真杀引擎杀出来的）；
        # 旧后端整个 `recover` 段缺位时 `maxAttempts` 也拿不到。网页版同一处写的是
        # `attempt || 1` / `maxAttempts ?? 5`（LobbyPage.tsx:459），设置页也是，此前只漏了这一页。
        attempt = int(recover.get("attempt") or 0) or 1
        mx = int(recover.get("maxAttempts") or 0) or 5
        return "recovering", (f"KataGo 断链，自愈中（第 {attempt} 次重试，"
                              f"最多 {mx} 次）")
    if not katago.get("available"):
        return "missing", "未检测到 KataGo，当前用内置启发式引擎（棋力较弱）"
    return "ready", "内置启发式引擎"


class LobbyPage(QWidget):
    """大厅。`openRequested(gameId)` 与 `gameStarted(gameId)` 由 shell 接。"""

    openRequested = Signal(str)
    reviewRequested = Signal(str)
    gameStarted = Signal(str)

    def __init__(self, api, parent=None):
        super().__init__(parent)
        self._api = api
        self._user: dict = {}
        self._games: list[dict] = []
        self._activeId: str = ""
        self._activeGame: dict = {}
        self._busy = False
        # 删记录/清历史在飞（与 `_busy` 分开：那一颗只管「对阵 AI」）。
        self._mutating = False
        # 当前处在二次确认的是哪一对按钮："del" / "clear" / None。只存一个值，
        # 所以进了一个确认态另一个会自动回到原样（两对同时显示确认钮会很混乱）。
        self._pending: str | None = None
        # 用户在“执子”里自己的选择。让子棋会强行把下拉框拨到黑，
        # 没有这个暂存值的话，改回分先后他原先选的“抽取/白”就丢了。
        self._colorChoice = 1
        self._startLabel = "对阵 AI"

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # ---- 顶部：欢迎语 + 徽章 + 引擎状态
        head = QHBoxLayout()
        self.title = QLabel("大厅", self)
        self.title.setProperty("role", "title")
        self.badge = QLabel("—", self)
        self.badge.setProperty("role", "badge")
        self.engineBadge = QLabel("引擎状态未知", self)
        self.engineBadge.setProperty("role", "badge")
        head.addWidget(self.title)
        head.addWidget(self.badge)
        head.addStretch(1)
        head.addWidget(self.engineBadge)
        root.addLayout(head)

        # ---- 统计卡：总场次 / 胜率 / 本级战绩 / 晋升进度
        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.statCards: dict[str, QLabel] = {}
        for key, label in (("record", "总战绩"), ("winrate", "总胜率"),
                           ("rank", "本级战绩"), ("lossPoints", "平均吻合度")):
            # 只在这里摆进布局。曾经 `_stat_card` 内部也 addWidget 了一次，
            # 于是一张卡占两个项（带 stretch 的那个 + 不带的那个），整行怎么铺都不对。
            cards.addWidget(self._stat_card(key, label), 1)
        root.addLayout(cards)

        promo = QFrame(self)
        promo.setProperty("card", True)
        pl = QVBoxLayout(promo)
        pl.setContentsMargins(14, 10, 14, 12)
        self.promoText = QLabel("晋升进度", promo)
        self.promoText.setProperty("role", "h3")
        self.promoBar = QProgressBar(promo)
        self.promoBar.setRange(0, 100)
        self.promoBar.setValue(0)
        # 10px 的槽里画不下「37%」这几个字（会被上下裁掉），网页版那条也是纯色槽；
        # 具体数字在 `promoText` 里给（见 `_paint_user`）。QSS 里写 `text: none` 不管事。
        self.promoBar.setTextVisible(False)
        self.promoHint = QLabel("", promo)
        self.promoHint.setProperty("role", "muted")
        self.promoHint.setWordWrap(True)
        pl.addWidget(self.promoText)
        pl.addWidget(self.promoBar)
        pl.addWidget(self.promoHint)
        root.addWidget(promo)

        # ---- 对局列表
        listHead = QHBoxLayout()
        self.listTitle = QLabel("最近对局", self)
        self.listTitle.setProperty("role", "h3")
        self.refreshBtn = QPushButton("刷新", self)
        self.refreshBtn.clicked.connect(self.refresh)
        #: 「查看复盘」放在列表这一侧（挨着它作用的对象），不跟破坏性的两颗混在一起。
        #: 之前只有「双击」这一条路能打开记录，而且打开的是**对局页** —— 用户报的
        #: 「点对局记录看不到复盘」就是这个：单击没反应、双击也没有复盘。
        self.btnReview = QPushButton("查看复盘", self)
        self.btnReview.setToolTip("选中一条已终局的对局，打开它的 AI 复盘。")
        self.btnReview.clicked.connect(self._open_review)
        listHead.addWidget(self.listTitle)
        listHead.addWidget(self.refreshBtn)
        listHead.addWidget(self.btnReview)
        listHead.addStretch(1)
        root.addLayout(listHead)

        self.games = QListWidget(self)
        self.games.itemDoubleClicked.connect(self._on_pick)
        self.games.currentItemChanged.connect(lambda *_: self._sync_ops())
        # 单条记录能点出什么，光看列表是看不出来的 —— 写明在 tooltip 里
        self.games.setToolTip("双击：已终局的对局直接看复盘；进行中的回到棋盘。")
        root.addWidget(self.games, 1)

        # ---- 记录操作：删除选中 / 清空历史（都只删明细，战绩不动）
        # 挤进「最近对局」那一行，不新开一行：大厅在 1280x800 下原本只剩 15px 余量，
        # 而一行按钮要 35px（`test_no_page_needs_a_scrollbar` 断的是 sizeHint，
        # 新开一行页面就会开始纵向滚动）。
        # 这一行放得下：六颗里同时可见的最多三颗（确认态与入口态互斥），最宽的一态是
        # 「标题 + 刷新 + 一句说明 + 确认删除 + 取消 + 清空记录」≈ 750px，而内容宽有 1112px。
        # 破坏性的两颗排在最右，与「刷新」隔一个 stretch：挨着放容易误点。
        listHead.setSpacing(10)
        self.btnDelSel = QPushButton("删除选中记录（保留战绩）", self)
        self.btnDelOk = QPushButton(DEL_OK, self)
        self.btnDelOk.setProperty("role", "danger")
        self.btnDelCancel = QPushButton("取消", self)
        self.btnClear = QPushButton("清空记录（保留战绩）", self)
        self.btnClearOk = QPushButton(CLEAR_OK, self)
        self.btnClearOk.setProperty("role", "danger")
        self.btnClearCancel = QPushButton("取消", self)
        self.opsHint = QLabel("", self)
        # 用 muted 不用 badge：对局页同一位置的 `forceNote`（“这一局作废……”）就是 muted，
        # 而顶着一枚蓝胶囊会被人当成另一个可点的东西（本应用给徽章定过一次同样的规矩）。
        self.opsHint.setProperty("role", "muted")
        for b in (self.btnDelOk, self.btnDelCancel, self.btnClearOk,
                  self.btnClearCancel, self.opsHint):
            b.setVisible(False)
        for b in (self.btnDelSel, self.btnDelOk, self.btnDelCancel, self.opsHint,
                  self.btnClear, self.btnClearOk, self.btnClearCancel):
            listHead.addWidget(b)
        # 二次确认与对局页的认输/强制结束同一口径：点一下先变成「确认 / 取消」，
        # 不发请求。删的是一个账号下全部对局明细，用户点错一下不该没有回头的机会。
        self.btnDelSel.clicked.connect(lambda: self._set_confirm("del"))
        self.btnDelCancel.clicked.connect(lambda: self._set_confirm(None))
        self.btnClear.clicked.connect(lambda: self._set_confirm("clear"))
        self.btnClearCancel.clicked.connect(lambda: self._set_confirm(None))
        # 真发请求的两颗只能接成绑定方法，不能接 lambda（见 `core/api.py` 里
        # `Reply` 的 docstring：接 lambda 会静默收不到回包，0/8）。
        self.btnDelOk.clicked.connect(self._confirm_delete)
        self.btnClearOk.clicked.connect(self._confirm_clear)
        self._sync_ops()   # 构造完就是一个空列表：两颗入口按钮得从第一帧就是暗的

        self.notice = QLabel("", self)
        self.notice.setProperty("role", "muted")
        root.addWidget(self.notice)

        # 开新局插在“晋升进度”与“对局列表”之间（与网页版同一顺序）。
        # 用 insertWidget 而不是重排上面：统计卡那一行的布局下标已被测试钉住。
        root.insertWidget(3, self._build_create())

    # ---------------------------------------------------------------- 组装

    def _stat_card(self, key: str, label: str) -> QFrame:
        """只负责建卡，**不摆进布局** —— 摆哪儿、给多大拉伸由调用方说。"""
        card = QFrame(self)
        card.setProperty("card", True)
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 10, 14, 12)
        v.setSpacing(2)
        cap = QLabel(label, card)
        cap.setProperty("role", "muted")
        value = QLabel("—", card)
        value.setProperty("role", "stat")
        v.addWidget(cap)
        v.addWidget(value)
        self.statCards[key] = value
        return card

    # ---------------------------------------------------------------- 开新局

    def _field(self, parent: QWidget, caption: str, items: list[tuple[str, object]],
               index: int = 0, width: int = 130) -> QComboBox:
        """一个下拉选项：上面一行小标题，下面一个 QComboBox（网页版 .field 的形状）。"""
        box = QVBoxLayout()
        box.setSpacing(2)
        cap = QLabel(caption, parent)
        cap.setProperty("role", "muted")
        box.addWidget(cap)
        cb = QComboBox(parent)
        for text, value in items:
            cb.addItem(text, value)
        cb.setCurrentIndex(index)
        cb.setFixedWidth(width)
        box.addWidget(cb)
        self._fieldBoxes.append(box)
        return cb

    def _build_create(self) -> QWidget:
        card = QFrame(self)
        card.setProperty("card", True)
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)
        self._fieldBoxes: list[QVBoxLayout] = []

        title = QLabel("开始新对局", card)
        title.setProperty("role", "h3")
        v.addWidget(title)

        self.activeBar = Alert("info", "继续对局", card)
        self.activeBar.button.clicked.connect(lambda: self.openRequested.emit(self._activeId))
        v.addWidget(self.activeBar)

        row = hbox(10)
        self.cbSize = self._field(card, "棋盘", [("9 路（入门）", 9),
                                                 ("13 路（进阶）", 13),
                                                 ("19 路（正式）", 19)], 2)
        self.cbKomi = self._field(card, "贴目", [("7.5（中国）", 7.5),
                                                 ("6.5（日韩）", 6.5),
                                                 ("0.5", 0.5),
                                                 ("0（让子棋）", 0)], 0, 120)
        self.cbHandicap = self._field(card, "让子",
                                      [("分先", 0)] + [(f"{h} 子", h) for h in range(2, 10)],
                                      0, 110)
        self.cbColor = self._field(card, "执子", [("抽取（猜先）", 0), ("黑（先行）", 1),
                                                  ("白（后行）", 2)], 1, 120)
        self.cbScore = self._field(card, "计分", [("数子（中国）", "area"),
                                                  ("数目（日韩）", "territory")], 0, 140)
        self.cbTime = self._field(card, "每手限时", [("30 秒", 30), ("60 秒", 60),
                                                     ("2 分钟", 120), ("5 分钟", 300),
                                                     ("不限时", 0)], 1, 120)
        for box in self._fieldBoxes:
            row.addLayout(box)
        row.addStretch(1)
        v.addLayout(row)

        foot = hbox()
        # 网页版这里是 `<label className="switch"><input type="checkbox">`（LobbyPage.tsx:327），
        # 也就是一个**真复选框**。原先这里用了个可勾选的 ghost 按钮，而 QSS 里的 ghost
        # 只有基础态与 :hover、没有 :checked —— 点下去勾上了，按钮长得和没勾时一模一样，
        # 用户完全无从确认「提示模式到底开没开」。换成 QCheckBox：既有天生的两态，
        # 也与本应用其余 7 处开关（设置页、复盘页、死活题页）口径一致。
        self.chkHintMode = QCheckBox("提示模式（每手显示引擎推荐点）", card)
        self.chkHintMode.setChecked(True)
        self.btnStart = QPushButton("对阵 AI", card)
        self.btnStart.setProperty("role", "primary")
        foot.addWidget(self.chkHintMode)
        foot.addStretch(1)
        foot.addWidget(self.btnStart)
        v.addLayout(foot)

        self.createNote = QLabel("", card)
        self.createNote.setProperty("role", "muted")
        self.createNote.setWordWrap(True)
        v.addWidget(self.createNote)

        # 让子棋固定玩家执黑：与网页版一样只**禁用 + 显示**黑，不抹掉用户原来的选择，
        # 改回分先时他自己选的“抽取/白”会自己回来。
        self.cbHandicap.currentIndexChanged.connect(self._sync_create_form)
        self.cbColor.currentIndexChanged.connect(self._sync_create_form)
        self.cbTime.currentIndexChanged.connect(self._sync_create_form)
        self.btnStart.clicked.connect(self._start_game)
        self._sync_create_form()
        return card

    def _sync_create_form(self) -> None:
        handicap = int(self.cbHandicap.currentData())
        forced = handicap >= 2
        # 只要下拉可编辑，走到这一趟就是用户的意图，先存下来。
        # 旧代码多了一个 `forced and` ：只在让子那一支才存，而函数尾部无条件按
        # `_colorChoice` 把下拉拨回 target —— 后果是分先下选「抽取（猜先）」会被
        # 同一个 `currentIndexChanged` 立即拨回「黑（先行）」，选「白」也被吞：
        # 用户看到的症状就是「猜先功能不可用」（第 20 轮用户报的）。
        # 程序性改动被 `blockSignals` 包住，不会重入本函数把暂存值污染掉。
        if self.cbColor.isEnabled():
            self._colorChoice = int(self.cbColor.currentData())
        # 让子棋固定玩家执黑：只把下拉框拨到黑并禁用，不改 `_colorChoice`；
        # 回到分先时再把它恢复回去。blockSignals 是因为这次改选不是用户意图，
        # 不能反过来再走一遍本函数（会把刚存下的选择当成新选择覆盖掉）。
        target = 1 if forced else self._colorChoice
        if int(self.cbColor.currentData()) != target:
            self.cbColor.blockSignals(True)
            self.cbColor.setCurrentIndex(target)      # 选项值恰好就是项号 0/1/2
            self.cbColor.blockSignals(False)
        self.cbColor.setEnabled(not forced)
        color = int(self.cbColor.currentData())
        notes = []
        if forced:
            notes.append("让子棋固定由玩家执黑先行，贴目建议设为 0.5。")
        elif color == 0:
            notes.append("执子由服务端随机抽取（猜先），结果开局后显示在棋盘上方；"
                         "抽到白则由 AI 先走。")
        seconds = int(self.cbTime.currentData())
        if seconds > 0:
            notes.append(f"每手 {seconds} 秒内未落子判超时负；超时以服务器时钟为准。")
        self.createNote.setText("\n".join(notes))
        self.createNote.setVisible(bool(notes))

    def set_active(self, game: dict | None) -> None:
        """shell 把 /api/games/active 的结果递过来：有未下完的局就不能再开新的一盘。"""
        self._activeGame = game or {}
        self._activeId = (game or {}).get("id", "")
        if game:
            self.activeBar.show_text(
                f"你有进行中的对局（{game.get('size', 19)} 路，第 {game.get('moveCount', 0)} 手）。",
                "info")
        else:
            self.activeBar.show_text("")
        self._sync_start_enabled()

    def _sync_start_enabled(self) -> None:
        self.btnStart.setEnabled(not self._busy and not self._activeId)
        # 开局在飞时也别去删记录：两个写操作同时跑，回包到的顺序谁也说不准。
        self._sync_ops()

    def _start_game(self) -> None:
        if self._busy or self._activeId:
            return
        handicap = int(self.cbHandicap.currentData())
        body = {
            "size": int(self.cbSize.currentData()),
            "komi": float(self.cbKomi.currentData()),
            "handicap": handicap,
            "playerColor": 1 if handicap >= 2 else int(self.cbColor.currentData()),
            "scoreMethod": str(self.cbScore.currentData()),
            "hintMode": self.chkHintMode.isChecked(),
            "moveSeconds": int(self.cbTime.currentData()),
        }
        self._busy = True
        self.btnStart.setText("创建中…")
        self._sync_start_enabled()
        self._api.post("/api/games", body).finished.connect(self._on_created)

    def _on_created(self, data, err) -> None:
        self._busy = False
        self.btnStart.setText(self._startLabel)
        self._sync_start_enabled()
        if err is not None or not data or not (data.get("game") or {}).get("id"):
            self.notice.setText(f"开新局失败：{getattr(err, 'args', [err])[0] if err else data}")
            return
        self.gameStarted.emit(str(data["game"]["id"]))

    # ---------------------------------------------------------------- 删记录

    def _selected_id(self) -> str:
        """选中那一盘的 id。占位项（「还没有对局。」）不带 data，拿到的是空串。"""
        item = self.games.currentItem()
        return str(item.data(Qt.UserRole) or "") if item is not None else ""

    def _game(self, game_id: str) -> dict:
        return next((g for g in self._games if str(g.get("id")) == str(game_id)), {})

    def _review_target(self) -> str:
        """选中那盘的 id —— 只在**已终局**时给出（进行中的局没有复盘可看）。"""
        gid = self._selected_id()
        return gid if gid and self._game(gid).get("finished") else ""

    def _open_review(self) -> None:
        gid = self._review_target()
        if gid:
            self.reviewRequested.emit(gid)

    def _sync_ops(self) -> None:
        """这一行按钮的可用与可见，全部从 (`_pending`, `_busy`/`_mutating`, `_games`) 推。

        可见性也走这里而不是只在 `_set_confirm` 里改一次：请求已经在飞的时候
        「取消」必须藏起来 —— 删除已经发出去了，给按下去什么都不做的一颗「取消」
        是骗人的（对局页的认输没这个区别，因为那边确认就是发 WS 指令）。
        """
        dele = self._pending == "del"
        clear = self._pending == "clear"
        idle = not self._busy and not self._mutating
        has = bool(self._games) and idle
        self.btnDelSel.setVisible(not dele)
        self.btnDelSel.setEnabled(has and bool(self._selected_id()))
        self.btnReview.setEnabled(has and bool(self._review_target()))
        self.btnDelOk.setVisible(dele)
        self.btnDelOk.setEnabled(idle)
        self.btnDelCancel.setVisible(dele and idle)
        self.btnClear.setVisible(not clear)
        self.btnClear.setEnabled(has)
        self.btnClearOk.setVisible(clear)
        self.btnClearOk.setEnabled(idle)
        self.btnClearCancel.setVisible(clear and idle)
        self.opsHint.setText(CONFIRM_HINT.get(self._pending or "", ""))
        self.opsHint.setVisible(self._pending is not None)

    def _set_confirm(self, kind: str | None) -> None:
        """只想换个确认态。按钮怎么摆不在这儿 —— 一律由 `_sync_ops` 从状态推出来。"""
        self._pending = kind
        self._sync_ops()

    def _confirm_delete(self) -> None:
        """不带 id 就静默返回：列表刚被刷新掉、占位项被选中都算这种情况，
        弹一个「删除失败：」的错误比什么都不说更让人发毛。"""
        gid = self._selected_id()
        if not gid or self._mutating:
            return
        self._mutating = True
        self.btnDelOk.setText("删除中…")
        self._sync_ops()
        self._api.delete(f"/api/games/{gid}").finished.connect(self._on_mutated)

    def _confirm_clear(self) -> None:
        if self._mutating:
            return
        self._mutating = True
        self.btnClearOk.setText("清空中…")
        self._sync_ops()
        self._api.post("/api/games/clear-history").finished.connect(self._on_mutated)

    def _on_mutated(self, data, err) -> None:
        """两个端点同一个收尾。靠 `self._pending` 认回包来自哪一颗 —— 发请求期间
        它不会变（「取消」已在飞的时候被 `_sync_ops` 藏了起来），所以不用给回包
        带个 kind 参数（那只能接成 lambda，而 `Reply.finished` 不许接 lambda）。"""
        kind = self._pending
        self._mutating = False
        if kind == "clear":
            self.btnClearOk.setText(CLEAR_OK)
        else:
            self.btnDelOk.setText(DEL_OK)
        self._set_confirm(None)
        if err is not None or not isinstance(data, dict):
            self.notice.setText(f"{'清空' if kind == 'clear' else '删除'}失败：{err_text(err)}")
            self._sync_ops()
            return
        # 已知取舍：这句占的是页面唯一那条消息行 `self.notice`。若此刻恰有一盘
        # 未结束的局，随后 `refresh()` 的 `_on_active` 会拿「有一盘未下完…」把它
        # 覆盖掉 —— 可接受：那个信息本来就在 `activeBar` 上重复摆着，而刚删完
        # 必须立刻看到的那句是「战绩还在」。顺序也是为此：先报删完，再拉新数据。
        self.notice.setText(stats_note(data))
        self.refresh()

    # ---------------------------------------------------------------- 数据

    def refresh(self) -> None:
        """四个 GET 并发拉。每个各自回填，谁先到谁先显示 —— 不必等最慢的那个。"""
        self._api.get("/api/auth/me").finished.connect(self._on_user)
        self._api.get("/api/games", {"limit": 30}).finished.connect(self._on_games)
        self._api.get("/api/system/status").finished.connect(self._on_status)
        self._api.get("/api/games/active").finished.connect(self._on_active)

    def _on_user(self, data, err) -> None:
        if err is not None or not data:
            self.notice.setText(f"读取账号失败：{err}")
            return
        self._user = data.get("user") or {}
        self._paint_user()

    def _on_games(self, data, err) -> None:
        if err is not None or not data:
            self.notice.setText(f"读取对局列表失败：{err}")
            return
        self._games = data.get("items") or []
        self._paint_games()

    def _on_status(self, data, err) -> None:
        if err is not None or not data:
            return
        kind, text = engine_state(data)
        self.engineBadge.setText(text)
        self.engineBadge.setStyleSheet(theme.badge_style(ENGINE_KIND.get(kind, "muted")))

    def _on_active(self, data, err) -> None:
        if err is not None or not data:
            return
        game = data.get("game")
        self.set_active(game)
        if game:
            self.notice.setText(
                f"有一盘未下完的对局（{game.get('rankName', '')} · "
                f"{game.get('moveCount', 0)} 手），双击列表或点上方的「继续对局」回去。")

    # ---------------------------------------------------------------- 绘制

    def _paint_user(self) -> None:
        p = self._user.get("progress") or {}
        name = self._user.get("displayName") or self._user.get("username") or ""
        self.title.setText(f"{name} 的大厅")
        self.badge.setText(f"{p.get('rankName', '—')}（{p.get('short', '')}）")
        self.badge.setStyleSheet(theme.badge_style("promo" if p.get("inPromotion") else ""))
        # 按钮上直接写出对手名字：开一局之前就该知道要对的是谁
        if p.get("aiName"):
            self._startLabel = f"对阵 {p['aiName']}"
            if not self._busy:
                self.btnStart.setText(self._startLabel)
        self.set_active(dict(self._activeGame) if self._activeGame else None)

        total = int(p.get("totalGames") or 0)
        wins = int(p.get("totalWins") or 0)
        # 口径抄网页版：负 = 总场次 - 胜。无胜负局（强制结束、导入棋谱）在
        # 这里会被当成"负"，那是旧版就有的算法，本轮不悄悄改口径。
        self.statCards["record"].setText(f"{wins} 胜 / {max(0, total - wins)} 负")
        self.statCards["winrate"].setText(f"{wins / total * 100:.0f}%" if total else "—")
        self.statCards["rank"].setText(
            f"{p.get('rankWins', 0)} 胜 / {p.get('rankLosses', 0)} 负")
        acc = p.get("avgLossPoints")
        self.statCards["lossPoints"].setText(f"{acc} 目/手" if acc not in (None, "") else "—")

        need = max(1, int(p.get("winsRequired") or 1))
        got = int(p.get("rankWins") or 0)
        if p.get("inPromotion"):
            done = int(p.get("promotionWins") or 0)
            req = max(1, int(p.get("promotionRequired") or 1))
            self.promoText.setText(f"晋升战：{done} / {req} 胜")
            motion.animate_value(self.promoBar, "value", int(done / req * 100), "bar")
        else:
            self.promoText.setText(f"本级累计：{got} / {need} 胜可打晋升战")
            motion.animate_value(self.promoBar, "value", min(100, int(got / need * 100)), "bar")
        self.promoHint.setText(p.get("hint") or "")

    def _paint_games(self) -> None:
        # 重建前先记住选中哪一盘：`clear()` 会把选中连同高亮一起抹掉，而
        # 「查看复盘 / 删除选中记录」的可用性跟着选中走 —— 每次列表刷新都
        # 把它们变回双灰（审计 L5）。
        keep = self._selected_id()
        self.games.clear()
        if not self._games:
            item = QListWidgetItem("还没有对局。点「开始新对局」下一盘。", self.games)
            item.setForeground(theme.color(theme.MUTED))
            self.games.setEnabled(False)
            self._sync_ops()      # 没得删了，那两颗按钮必须跟着暗下去
            return
        self.games.setEnabled(True)
        for g in self._games:
            flags = []
            if g.get("isPromotion"):
                flags.append("晋升战")
            if g.get("reviewStatus") == "pending":
                flags.append(f"复盘中 {int(float(g.get('reviewProgress') or 0) * 100)}%")
            elif g.get("reviewStatus") == "done":
                flags.append("有复盘")
            state = "已终局" if g.get("finished") else "进行中"
            color_txt = {1: "黑", 2: "白"}.get(g.get("playerColor"), "")
            if color_txt and g.get("colorSource") == "guess":
                color_txt += "（猜先）"
            mid = f" · {color_txt}" if color_txt else ""
            text = (f"{g.get('rankName', '')} · {g.get('size', 19)}路{mid} · "
                    f"{g.get('resultText') or state}"
                    + (f"   [{' / '.join(flags)}]" if flags else ""))
            item = QListWidgetItem(text, self.games)
            item.setData(Qt.UserRole, g.get("id", ""))
            won = bool(g.get("playerWon"))
            if g.get("finished"):
                item.setForeground(theme.color(theme.OK if won else theme.DANGER))
        # 把选中还回去（审计 L5）。找不到（那一盘被删了 / 换了筛选）就保持无选中，
        # 让「删除选中记录」诚实地暗着。
        if keep:
            for i in range(self.games.count()):
                if str(self.games.item(i).data(Qt.UserRole) or "") == keep:
                    self.games.setCurrentRow(i)
                    break
        # 列表一重画，选中项必定被 `clear()` 抹掉了 —— 操作行得跟着重新推一次，
        # 否则「删除选中记录」会在一个根本没有选中的列表上亮着。
        self._sync_ops()

    def _on_pick(self, item) -> None:
        """双击一条记录：已终局 → 直接看复盘；进行中 → 回棋盘接着下。"""
        if not item:
            return
        gid = item.data(Qt.UserRole)
        if not gid:
            return
        if self._game(gid).get("finished"):
            self.reviewRequested.emit(str(gid))
        else:
            self.openRequested.emit(str(gid))
