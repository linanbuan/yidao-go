"""外壳 + 登录页 + 大厅的端到端验收：真起窗口、真点按钮、真发请求。

计划里 P1 的验收口径有两条硬指标，都钉在这里：
  · 「无终端出窗口并能登录」→ QTest 真点「注册新账号」，等 shell 切进主界面；
  · 「在 1280x800 与 1707x960 两档下**无滚动条**」→ 页面 sizeHint/minimumSize
    必须装得进内容视口，用断言而不是"我看着没滚动条"。
"""
from __future__ import annotations

import time
import uuid

import pytest
from PySide6.QtCore import Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea, QWidget

from core import backend_host as bh
from core.api import ApiClient, Reply
from core.settings import Prefs
from core.sound import SoundPlayer
from ui import chrome as chrome_mod
from ui import shell as shell_mod
from ui.pages import game as game_page
from ui.pages import lobby, login
from tests import harness as H

PASSWORD = "mimashou123"


def drain(qapp, predicate, timeout: float = 15.0) -> bool:
    """转事件循环直到条件成立（跨线程投递不是在 processEvents 的一瞬到达的）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture(scope="module")
def host():
    h = bh.BackendHost()
    h.start(timeout=90.0)
    yield h
    h.stop()


@pytest.fixture
def shell(qapp, host, tmp_path):
    """一个真窗口。Prefs 指到 tmp：测试写 token 不许碰开发者自己的 AppData。"""
    prefs = Prefs(str(tmp_path / "client.ini"))
    sh = shell_mod.Shell(host, prefs)
    sh.resize(1280, 800)
    sh.show()
    QApplication.processEvents()
    yield sh
    sh.close()
    sh.deleteLater()


# ---------------------------------------------------------------- 登录

def test_starts_on_login_page(shell, qapp):
    assert shell._stack.currentWidget() is shell._login
    assert isinstance(shell.current_page(), (login.LoginPage, lobby.LobbyPage))
    assert shell.isVisible()


def test_the_login_card_says_whats_next_for_a_new_student(shell, qapp):
    """登录页那句说明该是给学员看的晋升路径，不是文件放在哪个目录。

    口径逐字对齐 `frontend/src/pages/LoginPage.tsx:73`（数字与
    `backend/app/rank/defs.py` 的 `wins_required=3 / promo_streak=2` 对得上）。
    这一条是换上真字体重读截图才抓到的：之前那张图里所有字都是方框，
    「数据都在 backend/data」这句开发者视角的话就这么混过去了。
    """
    tip = shell._login.tip.text()
    assert "18级" in tip and "晋升战" in tip, tip
    assert "/" not in tip and "\\" not in tip, f"登录页在给学员看文件路径：{tip}"


def test_click_register_logs_in_and_lands_on_lobby(shell, qapp):
    username = "sh" + uuid.uuid4().hex[:8]
    QTest.keyClicks(shell._login.user, username)
    QTest.keyClicks(shell._login.pw, PASSWORD)
    QTest.mouseClick(shell._login.btnRegister, Qt.LeftButton)

    ok = drain(qapp, lambda: shell._stack.currentIndex() == 1)
    assert ok, f"点注册后没进主界面，页面上的错误是：{shell._login.error.text()!r}"
    assert shell._prefs.token, "令牌没落盘"
    # 标题只报应用名；用户名在设置页右上角（2026-09-08 起）
    assert shell.windowTitle() == chrome_mod.APP_TITLE, shell.windowTitle()

    page = shell.current_page()
    assert isinstance(page, lobby.LobbyPage)
    assert drain(qapp, lambda: page.statCards["record"].text() != "—"), \
        f"大厅统计没回填，提示条是：{page.notice.text()!r}"
    # 新账号必定从 18 级开始：这是等级体系的起点，也是"数据真的通了"的证据
    assert "18" in page.badge.text(), page.badge.text()
    assert page.statCards["record"].text() == "0 胜 / 0 负"


def test_wrong_password_stays_on_login(shell, qapp):
    QTest.keyClicks(shell._login.user, "bu_cun_zai_yong_hu")
    QTest.keyClicks(shell._login.pw, "cuowucuowu")
    QTest.mouseClick(shell._login.btnLogin, Qt.LeftButton)
    assert drain(qapp, lambda: shell._login.error.isVisible())
    assert shell._stack.currentIndex() == 0
    assert shell._login.error.text(), "错误文案是空的"


def test_stale_token_falls_back_to_login(qapp, host, tmp_path):
    prefs = Prefs(str(tmp_path / "client.ini"))
    prefs.token = "not-a-real-jwt"
    sh = shell_mod.Shell(host, prefs)
    sh.show()
    sh.boot()
    # 等的是"令牌被清掉"这个副作用，不是等页面切换 —— 登录页本来就是当前页，
    # 拿 currentIndex == 0 当条件是恒真的，那样这条测试什么也没断到。
    assert drain(qapp, lambda: prefs.token == ""), "失效令牌没被清掉"
    assert sh._stack.currentIndex() == 0
    assert "失效" in sh.status.text(), sh.status.text()
    sh.close()


def _register(shell, qapp, prefix="ak") -> str:
    username = prefix + uuid.uuid4().hex[:8]
    QTest.keyClicks(shell._login.user, username)
    QTest.keyClicks(shell._login.pw, PASSWORD)
    QTest.mouseClick(shell._login.btnRegister, Qt.LeftButton)
    assert drain(qapp, lambda: shell._stack.currentIndex() == 1), \
        f"注册没进去：{shell._login.error.text()!r}"
    return username


def test_login_clears_the_boot_hint(shell, qapp):
    """登录成功后，boot 留下的“请先登录。”得从状态栏消失。

    看真窗口截图发现的：已经进去了、数据也回填了，左下角还挂着“请先登录。”。
    状态栏只有一条，旧文案不清就是假信息。
    """
    shell.boot()
    assert shell.status.text() == "请先登录。", shell.status.text()
    _register(shell, qapp)
    assert "登录" not in shell.status.text(), shell.status.text()


def test_401_bounces_back_to_login(shell, qapp):
    """令牌半路失效（过期 / 被清库）：送回登录页并清掉 token。

    不接这条的话，四个 GET 会各自在页面上挂一句“读取失败”，用户没有任何出路。
    """
    _register(shell, qapp, prefix="to")
    assert shell._prefs.token
    shell._prefs.token = "zhe-ge-token-yi-jing-zuo-fei"      # 当作已失效
    shell.go("lobby")                                        # refresh() → 四个 GET 全 401
    assert drain(qapp, lambda: shell._stack.currentIndex() == 0), "401 没把用户送回登录页"
    assert shell._prefs.token == "", "失效令牌没被清掉，下次启动还会再来一遍"
    assert "失效" in shell.status.text(), shell.status.text()


# ---------------------------------------------------------------- 布局预算

@pytest.mark.parametrize("w,h", [(1280, 800), (1707, 960)])
def test_no_page_needs_a_scrollbar(shell, qapp, w, h):
    """两档窗口下，每个页面的 sizeHint 与 minimumSize 都要装得进内容视口。

    用 minimumSize 兜底：sizeHint 是"想要多大"，minimumSize 才是"再小就画不下"。
    两个都不许超过视口，才是真的没有滚动条。

    **必须先真注册**：原先只 `_enter(假 user)` 没有令牌，各页 refresh 的 GET 全 401
    → 外壳 `_on_unauthorized` 把 stack 送回登录页 → `current_page()` 返回的是登录页，
    于是六个键量的都是同一个 412x271 的登录页，这条测试从 P1 起就是假绿
    （本轮加设置页时取证坐实，见 `_probe_scroll.py` 的输出）。现在既真登录，
    又逐页断言「量的确实是这一页」，再也不会静默换成别的控件。
    """
    _register(shell, qapp)
    shell.resize(w, h)
    QApplication.processEvents()
    # 先等布局真的把视口摆出来（offscreen 下 show() 可能改尺寸，量早了是两套坐标）
    assert drain(qapp, lambda: shell.content_size().width() > 200
                 and shell.content_size().height() > 200), \
        f"内容视口没真的摆出来：{shell.content_size().width()}x{shell.content_size().height()}"
    viewport = shell.content_size()
    checked = exempt = 0
    for key, _title, _phase in shell_mod.NAV:
        shell.go(key)
        assert drain(qapp, lambda: shell.current_page() is shell._pages.get(key)), \
            f"{key} 页没量上：当前显示的是 {type(shell.current_page()).__name__}"
        page = shell._pages[key]
        hint, minimum = page.sizeHint(), page.minimumSize()
        assert minimum.width() <= viewport.width(), \
            f"{key} 最小宽度 {minimum.width()} 超出视口 {viewport.width()}"
        assert minimum.height() <= viewport.height(), \
            f"{key} 最小高度 {minimum.height()} 超出视口 {viewport.height()}"
        assert hint.width() <= viewport.width(), \
            f"{key} 需要横向滚动：sizeHint {hint.width()} > {viewport.width()}"
        # 自带滚动区的页（对局/死活/复盘/设置，第 33 轮起设置页内容收进滚动区，
        # 它在 1280x800 下天然比视口高）纵向 sizeHint 可以大于视口：
        # 那一个维度正是它设计成可滚的地方，而且形状已经逐张看过截图
        # （page_06~09 / p3_0* / p4_0* / p5_*）。但**横向**不许超：横向没得滚，
        # 超了就是静默裁字。无滚动区的页（大厅/段位）仍按整页不滚的要求量。
        scrolls = [s for s in page.findChildren(QScrollArea)
                   if s.verticalScrollBarPolicy() != Qt.ScrollBarAlwaysOff]
        if scrolls:
            exempt += 1
            continue
        checked += 1
        assert hint.height() <= viewport.height(), \
            f"{key} 需要纵向滚动：sizeHint {hint.height()} > {viewport.height()}"
    assert checked >= 2, f"只量了 {checked} 个无滚动区的页，大厅/段位应该都在"
    assert exempt == 4, f"自带滚动区的页应有 4 个（对局/死活/复盘/设置），实际 {exempt}"


def test_stat_card_row_has_each_card_once(shell, qapp):
    """四张统计卡：每张只占一个布局项、等宽、铺满整行。

    这条是被截图逼出来的：`_stat_card` 内部 addWidget 一次、调用方又 addWidget 一次，
    一张卡占两个项（带 stretch 的那个 + 不带的那个），于是卡片停在 sizeHint
    宽度上、多出来的空间全变成卡间空隙，整行看着是散的。`count() == 4` 就是照妖镜。

    走 `_register` 而不是 `_enter`：没有令牌时 lobby 的四个 GET 会 401 把 stack 顶回
    登录页，下面的 `type(page).__name__ == "LobbyPage"` 就成了看运气的断言。
    """
    _register(shell, qapp)
    assert drain(qapp, lambda: shell.content_size().width() > 200)
    page = shell.current_page()
    assert type(page).__name__ == "LobbyPage"
    row = page.layout().itemAt(1).layout()
    assert row.count() == 4, f"统计卡那行有 {row.count()} 个项 —— 卡片被重复摆进布局了"
    widths = [row.itemAt(i).widget().width() for i in range(4)]
    assert max(widths) - min(widths) <= 2, f"四张卡不等宽：{widths}"
    right = row.itemAt(3).widget().geometry().right()
    assert right >= page.width() - 20, f"没铺满整行：右边界 {right}，页宽 {page.width()}"


def test_progress_bars_do_not_paint_text_in_the_groove(shell, qapp):
    """进度条槽内不许画字（10px 高的槽装不下 `37%`，会被上下裁）。

    QSS 里写 `text: none` 是没用的：Qt 不认这个属性，只往 stderr 丢一句
    `Unknown property text` 就丢掉，文字照旧画 —— 这一条就是顺着那 29 句噪音
    挖出来的（同批输出里还埋着一条更要紧的 `QLayout` 警告）。
    只能 `setTextVisible(False)`，而 `Shell` 是**懒建页**的，所以扫到的总数
    也要断言：不然「一页都没建、自然一个都没违规」会算成绿。
    """
    _register(shell, qapp)
    assert drain(qapp, lambda: shell.content_size().width() > 200)
    offenders, scanned = H.bar_texts(shell)
    assert scanned >= 1, "一个进度条都没扫到 —— 大厅页八成没建起来（假绿）"
    assert not offenders, "进度条槽内还在画字：" + "；".join(offenders)


# ---------------------------------------------------------------- 截图

def _brief(**over) -> dict:
    """对局列表的一行。键必须与 backend/app/game/manager.py 的 `record_brief` 对齐 ——
    写错一个不会报错，只会那一列静悄悄地空着。"""
    row = {
        "id": "g1", "createdAt": "2026-09-01T20:14:00", "size": 19, "komi": 7.5,
        "handicap": 0, "playerColor": 1, "rankId": 18, "rankName": "18级",
        "aiName": "小林一角", "isPromotion": False, "status": "finished",
        "finished": True, "finishReason": "score", "winner": 1,
        "resultText": "黑胜 3.5 目", "playerWon": True, "moveCount": 168,
        "reviewStatus": "done", "reviewProgress": 1.0, "reviewStage": "",
        "reviewDetail": "", "avgLossPoints": 2.4, "engine": "katago",
    }
    row.update(over)
    return row


def test_gauges_show_only_the_current_ply_analysis(qapp):
    """分析没跟上这一手时数字回到 `—`，不拿上一手的数字凑；`+/−` 与一位小数逐字对齐网页版。

    看 KataGo 那一支的中盘截图发现的矛盾：地盘热力图明明画着，「我方胜率」「目差」
    却是 `—`。取证结论是**两边一致、不是缺陷**：`GamePage.tsx:79` 取的也是
    `s.analyses[ply]`（ply 严格等于手数），`:226`/`:230` 空值给 `—`；而热力图用的是
    store 里那份粘性的 `s.ownership`（`:198`），所以上一手的地图 + 这一手的空数字
    在网页版同样成立，KataGo 下只是更容易看见（后台分析要真跑引擎）。
    钉住它有两个理由：① 下次看图不用再怀疑一遍；② 防“顺手改成取最近一条” ——
    那会把上一手的胜率标在当前手数下面，是指鹿为马，比空着更糟。

    不连 WS、不启后端：这一条量的是“数据缺位时界面怎么说话”，喂 state 帧就够。
    """
    page = game_page.GamePage(ApiClient(lambda: "http://127.0.0.1:1", lambda: ""))
    # `_paint()` 在没有对局时早退（它要画的是“还没有对局”那一屏），所以先给一个 id
    # 才走得到数字那一段。这个 id 不会真被用到：不连 WS、不发请求。
    page.game_id = "g-gauge-only"
    size = 9
    # 10 手摆在互不相邻的点上：不给 `board` 时页面会按手顺重放，挨着摆会把提子绕进来
    spots = [(x, y) for y in range(0, size, 2) for x in range(0, size, 2)]
    moves = [{"x": x, "y": y, "color": game_page.BLACK if i % 2 == 0 else game_page.WHITE}
             for i, (x, y) in enumerate(spots[:10])]
    one = {"winrateBlack": 0.62, "winrateWhite": 0.38, "scoreLead": 7.5}
    state = {"size": size, "moves": moves, "playerColor": game_page.BLACK,
             "nextColor": game_page.WHITE, "phase": "playing", "komi": 7.5,
             "analyses": [dict(one) for _ in range(10)]}
    page.load_from_payload(state)
    page._paint()
    # 10 手要的是 `analyses[10]`，上面只喂到 9：数字必须空着，不能拿 analyses[9] 凑
    assert page.gauge["moves"].text() == "10", page.gauge["moves"].text()
    assert page.gauge["winrate"].text() == "—", "缺这一手的分析却报了数，那是上一手的"
    assert page.gauge["score"].text() == "—", page.gauge["score"].text()

    # 补上当前这一手。三个格式化边界（正 / 负 / 零）皆钉在此处：网页版是
    # `${v > 0 ? '+' : ''}${v.toFixed(1)}` —— 零不带加号，负号由数值自己带。
    # 刻意避开 `.x5` 末位：JS toFixed 是 half-up、Python `:.1f` 是 half-even，
    # 0.25 两边会差 0.1（已记进日志，不影响任何判断，本轮不对齐）。
    state = {**state, "analyses": state["analyses"] + [dict(one)]}
    page.load_from_payload(state)
    page._paint()
    assert page.gauge["winrate"].text() == "62.0%", page.gauge["winrate"].text()
    assert page.gauge["score"].text() == "+7.5", page.gauge["score"].text()
    page.analyses[10]["scoreLead"] = -3.2
    page._paint()
    assert page.gauge["score"].text() == "-3.2", page.gauge["score"].text()
    page.analyses[10]["scoreLead"] = 0.0
    page._paint()
    assert page.gauge["score"].text() == "0.0", "零目差写 `+0.0` 与网页版不一致"
    # 白方视角：同一个黑视角 scoreLead 得取反，胜率也得换成 winrateWhite
    page.player_color = game_page.WHITE
    page.analyses[10]["scoreLead"] = 7.5
    page._paint()
    assert page.gauge["score"].text() == "-7.5", page.gauge["score"].text()
    assert page.gauge["winrate"].text() == "38.0%", page.gauge["winrate"].text()
    page.shutdown()


class _NoNetApi:
    """只记账、不碰网络的桩（给对局页喂事件用）。

    为什么不用真 `ApiClient` 指到一个没人监听的端口：那样会真起一个工作线程去跑
    一个必败的请求，回包抵达时页面往往已经拆了，测试输出里会多一条
    `RuntimeError: Signal source has been deleted`（错不在主线程，traceback 还不计入
    成败）。更要紧的是：这一条验的就是“有没有多发一个请求”，所以得有个账。

    `alive` 不是多余的：页面写的是 `api.get(p).finished.connect(self._on_review)`，
    而这个链里 Reply 只活在栈上 —— CPython 3.11+ 在 `LOAD_ATTR finished` 之后就把
    它弹掉了，没人接盘就是一个已析构的 signal source（实测：不接盘直接抛
    `RuntimeError: Signal source has been deleted`）。真客户端能这么写是因为
    `ApiClient._track()` 把它存进了 `_inflight`，桩得做同样的事。
    """

    def __init__(self):
        self.gets: list[str] = []
        # 开局与删记录要验的是「发出去的那个体对不对」，不只是一个路径名
        self.posts: list[tuple[str, object]] = []
        self.alive: list[Reply] = []

    def _reply(self, path: str) -> Reply:
        r = Reply()               # 永不 emit：这一条要的是“只发了一次”，不是回包
        self.alive.append(r)
        return r

    def get(self, path, query=None, timeout=20.0):
        self.gets.append(path)
        return self._reply(path)

    def post(self, path, body=None, query=None, timeout=20.0):
        self.posts.append((path, body))
        return self._reply(path)


def test_a_game_end_delivered_twice_still_announces_once(qapp, tmp_path):
    """同一条 gameEnd 到两次只能响一声 —— 强制结束就是会到两次。

    取证：`backend/app/game/manager.py` 的 `force_end()` 先 `live.emit(event)` 从 WS
    推一份，再把**同一个 dict** 塞进 REST 响应带回客户端（它不知道客户端有几条
    通道）。状态字段反复覆盖是幂等的，音效不是 —— 作废一局会听到两声点击，
    而真实终局（胜负/晋升音）双响更离谱。E2E 那条只在全量跑时偶发红，
    因为「第二声有没有赶在断言前落地」是时序问题 —— 这一条不起网络、
    直接把同一事件喂两次，把偶发变成必然。
    """
    sound = SoundPlayer(Prefs(str(tmp_path / "client.ini")))
    api = _NoNetApi()
    page = game_page.GamePage(api, sound)
    # 不连 WS 也能走终局绘制分支：`_paint()` 在 `game_id` 为空时早退（见上面那条测试）
    page.game_id = "g-end-twice"
    end = {"type": "gameEnd", "reason": "force-end", "winner": 0, "playerWon": False,
           "resultText": "强制结束（不计入胜负）", "countsForRank": False, "rank": None}
    page.apply_event(dict(end))
    assert sound.played == ("click",), f"作废只该响一声点击：{sound.played}"
    assert page.game_end == end and page.phase == "finished"
    assert len(api.gets) == 1, f"终局只该开一次复盘轮询：{api.gets}"

    page.apply_event(dict(end))          # 另一条通道送来的同一事件
    assert sound.played == ("click",), f"重复送达的 gameEnd 又响了一声：{sound.played}"
    assert page.game_end == end and page.phase == "finished", "幂等只该挡住副作用，状态还得落"
    assert len(api.gets) == 1, f"重复送达不该再多开一次轮询：{api.gets}"

    page.shutdown()
    # 复盘轮询也得一起停：不停的话这个页面会在后台再轮 200 次（每 3 秒一次），
    # 而 `shutdown()` 是退出路径上唯一的一个钩子。
    assert not page._review.isActive(), "shutdown 之后复盘定时器还在跑"


# ---------------------------------------------------------------- 开关的两态

def test_every_toggle_shows_a_different_picture_when_it_is_on(qapp, tmp_path):
    """每个开关都要让人看得出它是开着还是关着 —— 数两态的像素差，不靠眼看第二遍。

    缺陷是换上真字体重读大厅截图时看见的：「提示模式（每手显示引擎推荐点）」是个
    可勾选的 `QPushButton(role="ghost")`，而 `theme.QSS` 里 ghost 只有基础态与
    `:hover`，`QPushButton` 也没给 `:checked` 留规则。后果不是“难看”，是
    **状态丢了可见性**：点下去值确实变了、开局请求里也带对了，可屏幕上那个
    按钮长得和没点时一样 —— 学员下一眼就忘了自己到底开没开。
    对局页的「显示推荐点」「显示领地热力」是同一个病（只是它们走默认按钮样式）。

    实测（`scripts/_probe_toggle.py`）：`QCheckBox` 两态差 47 个像素（offscreen、
    DPR 1.0），而那两种可勾选的 `QPushButton` 两态都是 **0 个像素不同** ——
    修之前这一条必红，阈值拿 12（实测的四分之一）而不拿 1，是为了给
    换字体/换抗锯齿留余量，又不给“只差一两个降噪像素”蒙混过关的机会。

    修法为什么选 `QCheckBox` 而不是给按钮补一条 `:checked` 样式：网页版这三处
    本来就是 `.switch` 里的真 checkbox（`LobbyPage.tsx:327`、`GamePage.tsx:334/338`），
    而本应用其余 7 处开关也全是 `QCheckBox`。
    """
    api = _NoNetApi()
    ly = lobby.LobbyPage(api)
    gm = game_page.GamePage(api, SoundPlayer(Prefs(str(tmp_path / "client.ini"))))
    for w in (ly, gm):
        w.resize(1280, 800)
        w.show()
    qapp.processEvents()
    boxes = {"大厅·提示模式": ly.chkHintMode,
             "对局·显示推荐点": gm.chkHint,
             "对局·显示领地热力": gm.chkOwnership}
    for tag, box in boxes.items():
        differ, jitter = H.toggle_state_diff(box)
        assert jitter == 0, f"{tag} 同一个状态连抓两次都能差 {jitter} 个像素，这个测量不可信"
        assert differ >= 12, (f"{tag} 开着与关着只差 {differ} 个像素（实测 QCheckBox 是 47）："
                              f"这个开关看不出自己的状态")
    gm.shutdown()
    ly.deleteLater()
    gm.deleteLater()


def test_the_recommendation_choice_survives_a_new_game(qapp, tmp_path):
    """「显示推荐点」是玩家的观战选择，不随开新局重置。

    现场缺陷：`show_hint` 曾放在 `_reset_state` 里，每开一局都重置回 True，
    而复选框保持着上一局的勾选状态 —— 于是「上一局关掉推荐点、这一局照样
    显示」，开关停在关的位置却照常出推荐点；要再开再关一次才对得上
    （用户报的「没开启却一直显示」）。
    """
    gm = game_page.GamePage(_NoNetApi(), SoundPlayer(Prefs(str(tmp_path / "client.ini"))))
    assert gm.chkHint.isChecked() and gm.show_hint is True
    gm.chkHint.setChecked(False)
    assert gm.show_hint is False
    gm._reset_state()          # 开新局的必经之路（open_game 第一件事）
    assert gm.show_hint is False, "换局不该把观战开关重置回去"
    assert not gm.chkHint.isChecked(), "复选框与内部开关对不上"
    gm.shutdown()
    gm.deleteLater()


def test_a_hint_mode_event_disables_the_recommendations_mid_game(qapp, tmp_path):
    """设置页把「落子推荐」关掉 → 进行中的对局当场收口，不用等下一局。

    背靠后端的 `GameHub.set_hint_mode`（PATCH /api/auth/me 时向活对局广播
    `hintMode` 事件）；这里验的是页面这一侧的消费：事件到了，推荐点立刻
    不再画；再打开，恢复。
    """
    gm = game_page.GamePage(_NoNetApi(), SoundPlayer(Prefs(str(tmp_path / "client.ini"))))
    gm.game_id = "g-hintmode"        # 有局才走得到绘制那一段
    gm.apply_event({"type": "state", "state": {
        "size": 9, "moves": [], "komi": 7.5, "phase": "playing",
        "playerColor": 1, "nextColor": 1, "hintMode": True}})
    assert gm.meta["hintMode"] is True
    assert gm.is_my_turn
    gm.hint = [{"x": 3, "y": 3, "gtp": "D4", "winrate": 0.6}]
    gm._paint_board()
    assert len(gm.boardView._hints) == 1, "前提：开着的时候要真画推荐点"

    gm.apply_event({"type": "hintMode", "enabled": False})
    assert gm.meta["hintMode"] is False
    assert gm.boardView._hints == [], "hintMode 关了还画推荐点"

    gm.apply_event({"type": "hintMode", "enabled": True})
    assert gm.meta["hintMode"] is True
    assert len(gm.boardView._hints) == 1, "hintMode 打开后推荐点恢复"
    gm.shutdown()
    gm.deleteLater()


def test_the_game_page_first_frame_is_the_empty_state(qapp, tmp_path):
    """单独打开「对局」页（还没有任何对局）第一帧就该是空态。

    现场缺陷：`_paint()` 只在对局事件到来后才把「还没有对局」摆出来，
    而页面构造完没人调用它 —— 首帧是一屏没初始化过的控件（空棋盘 +
    一栏假按钮），看着像能玩其实什么都不能点（用户报的「对局页单独
    一面没有开启对局时会出逻辑 BUG」）。
    """
    gm = game_page.GamePage(_NoNetApi(), SoundPlayer(Prefs(str(tmp_path / "client.ini"))))
    gm.resize(1280, 800)
    gm.show()
    qapp.processEvents()
    assert gm.emptyHint.isVisible(), "没对局时该提示「还没有对局」"
    assert not gm.boardView.isVisible()
    assert not gm.scroll.isVisible(), "侧栏整条都该藏起来"
    gm.shutdown()
    gm.deleteLater()


def test_the_sidebar_chart_caption_names_both_lines(qapp, tmp_path):
    """侧栏那张图走紧凑档（图例被收掉换绘图区），颜色对应关系得有人接手。

    控件自己不知道页面那行说明写了什么，所以这一条得在页面上钉：
    哪天有人把 `chartHint` 改回「我方视角胜率与目差」，图例又不在，
    学员就只能猜哪条线是胜率。两边各自看都不像缺陷，合起来才是。

    （第 33 轮起无对局时侧栏整条隐藏 —— 图表不布局就没有真实高度，
    `compact` 是按实际高度算的，所以这里先喂一份曲线数据把侧栏点亮。）
    """
    gm = game_page.GamePage(_NoNetApi(), SoundPlayer(Prefs(str(tmp_path / "client.ini"))))
    gm.game_id = "g-chart"
    gm.curve = [{"ply": 0, "moveNum": 0, "winrateBlack": 0.5, "winrateWhite": 0.5,
                 "scoreLead": 0.0}]
    gm._paint()
    gm.resize(1280, 800)
    gm.show()
    qapp.processEvents()
    assert gm.chart.compact, "侧栏那张 150px 高的图应当走紧凑档（否则这条前提就变了）"
    text = gm.chartHint.text()
    assert "蓝" in text and "绿" in text, f"图例已收起，这行说明必须报出两条线的颜色：{text}"
    assert "胜率" in text and "目差" in text, text
    gm.shutdown()
    gm.deleteLater()


def test_screenshots_for_human_review(shell, qapp, tmp_path):
    """登录页 + 大厅（有数据 / 空列表）三张，我逐张看。"""
    username = "sn" + uuid.uuid4().hex[:8]
    H.snap(shell, "page_01_login")
    QTest.keyClicks(shell._login.user, username)
    QTest.keyClicks(shell._login.pw, PASSWORD)
    QTest.mouseClick(shell._login.btnRegister, Qt.LeftButton)
    assert drain(qapp, lambda: shell._stack.currentIndex() == 1)
    page = shell.current_page()
    assert drain(qapp, lambda: page.badge.text() != "—")

    # 从这里起由我们自己摆 `_games`，所以先把 `/api/games` 那条异步回包摘掉：
    # 它会 `self._games = data["items"]` 再重画，而回包可能在 `H.snap()` 内部的
    # `processEvents()` 里落地 —— 列表被清成空，上面那张「有数据」的截图就和下面
    # 那张「空列表」逐字节相同，断言在**全量套件里偶发**红、单独跑却永远绿
    # （实测：单跑 3/3 通过，全量 2/2 红）。本用例要验的是**渲染分支**，不是网络。
    page._on_games = lambda *a, **k: None

    # 刚注册的账号必定零对局，所以「有数据」那一支得自己端上去 —— 不然两张截图是同一张。
    # 四种行各自走不同的绘制分支：胜（绿）/ 负 + 晋升战 + 复盘中（红）/ 进行中（无色）/ 认负。
    page._games = [
        _brief(),
        _brief(id="g2", rankName="17级", isPromotion=True, playerWon=False,
               resultText="白胜 2.5 目", reviewStatus="pending", reviewProgress=0.42),
        _brief(id="g3", finished=False, status="playing", resultText=None,
               moveCount=31, reviewStatus="none", reviewProgress=0.0),
        _brief(id="g4", size=13, playerWon=False, resultText="中盘负",
               finishReason="resign", reviewStatus="failed"),
        _brief(id="g5", playerColor=2, colorSource="guess", playerWon=False,
               resultText="白胜 1.5 目", reviewStatus="done"),
    ]
    page._paint_games()
    assert page.games.count() == 5
    texts = [page.games.item(i).text() for i in range(5)]
    assert "有复盘" in texts[0], texts
    assert "晋升战" in texts[1] and "复盘中 42%" in texts[1], texts
    assert "进行中" in texts[2] and "目" not in texts[2], texts
    assert "13路" in texts[3] and "复盘" not in texts[3], texts
    # L15：执子信息 + 猜先徽章进行文本；自选/旧数据不带徽章（texts[0..3] 无 playerColor，格式不变）
    assert "白（猜先）" in texts[4] and "1.5 目" in texts[4], texts
    assert "（猜先）" not in texts[0], texts
    full = H.snap(shell, "page_02_lobby")

    # 空列表那一支也要看一眼：占位文案与有数据时的排版完全不同
    page._games = []
    page._paint_games()
    empty = H.snap(shell, "page_03_lobby_empty")
    assert page.games.item(0).text().startswith("还没有对局")
    # 两张必须真的不一样。曾经它们字节级相同（新账号本来就是空的），
    # 于是「看了三张截图」其实是看了两张 —— 有数据时的排版没人验过。
    assert full.read_bytes() != empty.read_bytes(), "两张大厅图一模一样，等于少看一张"


# ---------------------------------------------------------------- 大厅的引擎徽章

def _status(active="katago", warming=False, deaths=0, watching=True, attempt=0,
            max_attempts=5, available=None) -> dict:
    """按 `/api/system/status` 的真实形状造 engine 段（键名一个都不能简写）。

    `available` 默认跟着 `active` 走：真数据里这两者本来就是一致的（`active` 就是
    `katago if available else fallback.name`），手工摆界面时最容易摆出一个
    后端永远不会回的形状，那样的测试只在保护测试自己。
    """
    return {"engine": {
        "active": active, "warming": warming,
        "katago": {"available": available if available is not None else active == "katago",
                   "binary": r"D:\AI\围棋\backend\katago\katago.exe", "deathCount": deaths},
        "recover": {"watching": watching, "attempt": attempt, "maxAttempts": max_attempts}}}


@pytest.mark.parametrize("state,label", [
    ("ready", "KataGo 就绪"),
    ("warming", "预热中"),
    ("recovering", "自愈中"),
    ("missing", "内置启发式"),
])
def test_the_lobby_badge_gives_each_engine_state_its_own_words(state, label):
    """大厅那个徽章的四种说法。与设置页同一口径，但措辞短一档（它只有一行的位置）。

    为什么不复用设置页那组断言：两页各有一份 `engine_state`（一个返回 (kind, 文案),
    一个只返回 kind），把大厅那份并进去才是真的覆盖到它。
    """
    cases = {
        "ready": _status(),
        "warming": _status(active="heuristic", available=False, warming=True, watching=False),
        "recovering": _status(active="heuristic", available=False, deaths=1, attempt=2),
        "missing": _status(active="heuristic", available=False, watching=False),
    }
    kind, text = lobby.engine_state(cases[state])
    assert kind in lobby.ENGINE_KIND, kind
    assert label in text, f"{state} 态的文案是「{text}」"
    assert "未知" not in text, f"状态判出来了却还在说未知：{text}"


def test_the_first_retry_is_never_announced_as_the_zeroth(qapp):
    """断链后的头 5 秒里 `recover.attempt` 真的是 0 —— 那不是「第 0 次重试」。

    现场是 P5 验收真杀 katago.exe 杀出来的（`test_settings_katago.py`，那一支默认
    不跑，所以守卫必须落在这一支）：`pool._supervise` 先 `await asyncio.sleep(delay)`
    才把 `restart_count` 置成 1，所以每次断链都有一个 5 秒宽的窗口报 0。
    印出来是「自愈中（第 0 次重试，最多 5 次）」—— 一个用户能看懂的反话，
    而且它暗示还有一次没开始的重试在排队。网页版同一处写的是 `attempt || 1`
    （`LobbyPage.tsx:459`），设置页也是（`settings.py` 的 `or 1`），只有大厅漏了。
    """
    _kind, text = lobby.engine_state(_status(active="heuristic", available=False,
                                             deaths=1, attempt=0))
    assert "第 0 次" not in text, text
    assert "第 1 次" in text, f"第一次重试正在进行，文案却是：{text}"

    # 同一条判据管旧后端：整个 `recover` 段缺字段时不许说成「最多 0 次」
    _kind, sparse = lobby.engine_state({"engine": {"active": "heuristic",
                                                   "recover": {"watching": True}}})
    assert "0 次" not in sparse, sparse


# ---------------------------------------------------------------- 页面间接线

#: (哪一页, 它发的跳转信号, 参数, 期望落到哪一页)。验的是「发一次看落点」，
#: 不是问 Qt「这个信号有接收方吗」—— PySide 里 Python 侧的 connect 在
#: `QObject.receivers()` 上恒为 0（实测），问不出东西。
NAV_CASES: tuple[tuple[str, str, tuple, str], ...] = (
    ("game", "backRequested", (), "lobby"),
    ("game", "reviewRequested", ("navprobe01",), "review"),
    ("review", "backRequested", (), "lobby"),
    ("lobby", "openRequested", ("navprobe01",), "game"),
    ("lobby", "reviewRequested", ("navprobe01",), "review"),
    ("lobby", "gameStarted", ("navprobe01",), "game"),
)


def test_every_navigation_signal_is_wired(shell, qapp):
    """页面声明的每一个跳转信号都得有人接，而且要有测试钉着它。

    复盘页的 `backRequested` 漏接过一行 connect：「返回大厅」看着能点、点下去
    纹丝不动。这类缺陷在页面自己的测试里永远看不见 —— 页面只负责发信号，
    接不接是外壳的事；而 e2e 要跑完一整盘才撞得到它，太贵。"""
    _register(shell, qapp, prefix="nv")
    covered = {(key, name) for key, name, _a, _w in NAV_CASES}
    for key in ("lobby", "game", "tsumego", "ranks", "review", "settings"):
        page = shell._ensure_page(key, "")
        # 只看页面**自己声明**的那几个（`vars(cls)` 而不是 `dir(obj)`）：后者会把
        # Qt 基类的信号一起抓进来，`customContextMenuRequested` 不是跳转信号。
        declared = {n for n, v in vars(type(page)).items()
                    if isinstance(v, Signal)
                    and (n.endswith("Requested") or n == "gameStarted")}
        extra = declared - {n for k, n in covered if k == key}
        assert not extra, f"{key} 页还会发 {sorted(extra)}，但没有一条测试验它接没接"
    for key, name, args, want in NAV_CASES:
        shell.go(key)                                # 先站在源页上：落点才不是显然的
        qapp.processEvents()
        assert shell.current_page() is shell._pages[key], f"切到 {key} 页没成功"
        getattr(shell._pages[key], name).emit(*args)
        qapp.processEvents()
        assert shell.current_page() is shell._pages[want], \
            f"{key} 页发 {name} 没切到 {want}：外壳漏了一行 connect（症状是按钮点了没反应）"
    # 最后两条 case 会让两个页面去接一个不存在的局，收尾把它们拆干净
    shell._pages["game"].shutdown()
    shell._pages["review"].shutdown()


# ------------------------------------------- 第 20 轮：用户报的两个缺陷（猜先 / 删记录）

def test_the_color_combo_keeps_what_the_user_picked(qapp):
    """「执子」下拉必须听用户的 —— 修之前它**永远**被拨回暂存值。

    `_sync_create_form` 为了做「让子棋固定执黑」，每次进来都按 `_colorChoice`
    把下拉拨到 target；而 `_colorChoice` 过去**只在让子那一支才更新**。后果：
    分先下选「抽取（猜先）」→ `currentIndexChanged` → 同一个函数把它拨回
    「黑（先行）」，选「白」也被吞 —— 用户看到的症状就是「猜先功能不可用」。
    这个控件从来没有任何测试动过它（e2e 的 `_start` 只改棋盘大小与限时），
    所以一个完全不能用的下拉能一路活到 P5 交付。
    """
    ly = lobby.LobbyPage(_NoNetApi())
    api: _NoNetApi = ly._api
    ly.show()
    qapp.processEvents()
    assert ly.cbColor.currentData() == 1, "默认应当是「黑（先行）」"
    for value, tag in ((0, "抽取（猜先）"), (2, "白（后行）")):
        ly.cbColor.setCurrentIndex(ly.cbColor.findData(value))
        qapp.processEvents()
        assert ly.cbColor.currentData() == value, \
            f"选了{tag}，下拉自己跳回了 {ly.cbColor.currentData()}（用户改不动这个控件）"
        assert ly._colorChoice == value, f"{tag} 没被记成用户的选择，下一个下拉一动就会丢"
        if value == 0:
            assert "猜先" in ly.createNote.text(), \
                f"选了抽取却没给那句提示，用户无从确认抽没抽：{ly.createNote.text()!r}"
    # 动别的下拉不能把执子选择吞掉（`cbTime` 也连着同一个 sync）
    ly.cbTime.setCurrentIndex(ly.cbTime.findData(0))
    qapp.processEvents()
    assert ly.cbColor.currentData() == 2, "改「每手限时」把刚选的执子吞了"
    # 让子棋仍然强制执黑，但不许污染用户原来的选择
    ly.cbHandicap.setCurrentIndex(ly.cbHandicap.findData(2))
    qapp.processEvents()
    assert ly.cbColor.currentData() == 1 and not ly.cbColor.isEnabled(), "让子棋没固定执黑"
    ly.cbHandicap.setCurrentIndex(ly.cbHandicap.findData(0))
    qapp.processEvents()
    assert ly.cbColor.currentData() == 2 and ly.cbColor.isEnabled(), "回到分先后没拿回原来的选择"
    # 选完还得真的发出去：这一段是「下拉 → 请求体」。服务端抽取的契约是
    # `playerColor=0`（`backend/app/game/manager.py` 的 `draw_color`），递不进去
    # 就会变成“前端抽取” —— 而那是被否决的方案：用户可以反复抽到满意为止。
    ly.cbColor.setCurrentIndex(ly.cbColor.findData(0))   # 上面那一串已把下拉留在「白」
    qapp.processEvents()
    QTest.mouseClick(ly.btnStart, Qt.LeftButton)
    bodies = [b for p, b in api.posts if p == "/api/games"]
    assert bodies, f"点了「对阵 AI」却没发开局请求：{api.posts}"
    assert bodies[-1]["playerColor"] == 0, \
        f"选了抽取却没把 0 递到服务端，这局会变成用户自选执子：{bodies[-1]}"
    # 让子棋那一支反过来：不管下拉里存的是什么，发出去必须是黑（1）。
    # 桩永不回包，所以第一次点完要人工补一次回包收尾（否则 `_busy` 会把按钮禁掉，
    # 第二次点击什么都不会发 —— 那时这一条会绿在一个根本没发出去的请求上）。
    ly._on_created(None, None)
    ly.cbHandicap.setCurrentIndex(ly.cbHandicap.findData(2))
    QTest.mouseClick(ly.btnStart, Qt.LeftButton)
    bodies = [b for p, b in api.posts if p == "/api/games"]
    assert bodies[-1]["handicap"] == 2 and bodies[-1]["playerColor"] == 1, \
        f"让子棋把用户原先选的执白带进了请求：{bodies[-1]}"
    ly.deleteLater()


def test_every_field_on_the_create_card_reaches_the_request(qapp):
    """开局卡上六个下拉，每一个的值都必须真的出现在请求体里 —— 一个都不许靠眼看。

    起因就是上面那条猜先（§5-72）：改这一条之前的 `cbColor` 在全部 328 项桌面测试里
    被碰过 **0 次**（实测计数，不是印象），于是「下拉 → 请求体」断十九轮也没人看见。
    同一张卡上 `cbKomi`（贴目）与 `cbScore`（计分）在改之前同样是 0 次 —— 这一条把六个
    下拉一次性扫完，以后往这张卡上再加第七个，只要没写进 `fields` 就会红在名字上。
    """
    api = _NoNetApi()
    ly = lobby.LobbyPage(api)
    ly.show()
    qapp.processEvents()
    fields = {"棋盘": ("cbSize", "size"), "贴目": ("cbKomi", "komi"),
              "让子": ("cbHandicap", "handicap"), "执子": ("cbColor", "playerColor"),
              "计分": ("cbScore", "scoreMethod"), "每手限时": ("cbTime", "moveSeconds")}
    for cap, (attr, key) in fields.items():
        if ly.cbHandicap.currentIndex() != 0:
            # 每一趟先拿回分先：让子棋会强制执黑，那条规则不该来干扰别的字段
            ly.cbHandicap.setCurrentIndex(0)          # 项 0 就是「分先」，走真信号
            qapp.processEvents()
        cb = getattr(ly, attr)
        idx = 1 if cb.currentIndex() == 0 else 0       # 挑一个跟当前不同的项
        cb.setCurrentIndex(idx)
        qapp.processEvents()
        ly._on_created(None, None)   # 桩永不回包，得手工收掉上一次的「创建中…」
        QTest.mouseClick(ly.btnStart, Qt.LeftButton)
        bodies = [b for p, b in api.posts if p == "/api/games"]
        assert bodies, f"{cap} 这一趟连请求都没发出去：{api.posts}"
        want, got = cb.itemData(idx), bodies[-1][key]
        assert got == want, f"{cap} 选的是 {want!r}，请求体里却是 {got!r}（值没递进去）"
    # 这张卡上还有一个可勾选项（提示模式）：它不是下拉，但同样只有一条路进请求体。
    # 全测试套里 `chkHintMode` 此前只被像素那条改过一次，`hintMode` 这个字段从未
    # 从大厅这一侧进过任何断言（实测计数）。
    ly.chkHintMode.setChecked(not ly.chkHintMode.isChecked())
    ly._on_created(None, None)
    QTest.mouseClick(ly.btnStart, Qt.LeftButton)
    bodies = [b for p, b in api.posts if p == "/api/games"]
    assert bodies[-1]["hintMode"] is ly.chkHintMode.isChecked(), \
        f"提示模式勾的是 {ly.chkHintMode.isChecked()}，请求体里是 {bodies[-1]['hintMode']!r}"
    ly.deleteLater()


def _resign_a_new_game(shell, qapp) -> str:
    """大厅真开一局（9 路）→ 对局页真点认输到终局 → 返回大厅。返回那一局的 id。"""
    ly = shell.current_page()
    assert isinstance(ly, lobby.LobbyPage)
    ly.cbSize.setCurrentIndex(ly.cbSize.findData(9))
    QTest.mouseClick(ly.btnStart, Qt.LeftButton)
    assert H.wait(qapp, lambda: isinstance(shell.current_page(), game_page.GamePage),
                  timeout=40.0), "点了「对阵 AI」没进对局页"
    gp = shell.current_page()
    assert H.wait(qapp, lambda: gp.status == "open" and gp.phase == "playing",
                  timeout=60.0), f"首帧没落进来：{gp.status}/{gp.phase}"
    QTest.mouseClick(gp.btnResign, Qt.LeftButton)
    QTest.mouseClick(gp.btnResignOk, Qt.LeftButton)
    assert H.wait(qapp, lambda: gp.game_end is not None, timeout=60.0), "认输没到终局"
    gid = str(gp.game_id)
    QTest.mouseClick(gp.btnLobby, Qt.LeftButton)
    assert H.wait(qapp, lambda: isinstance(shell.current_page(), lobby.LobbyPage),
                  timeout=20.0), "认输后回不了大厅"
    return gid


def test_records_are_deletable_and_the_record_line_survives(shell, qapp):
    """大厅要能删记录，而删完**战绩不许动**（用户点名要保留胜率）。

    网页版早就有这一对（`LobbyPage.tsx:194/206`），桌面端 P1 时写了句
    「删除/清历史本轮仍不做」就一直没做，而它**没进 14.9 的欠账清单** ——
    写在代码 docstring 里的「不做」等于没记。后端两个端点一直在，
    本来就只删明细不动聚合（§4「删除记录不动聚合字段」）。
    """
    _register(shell, qapp, prefix="dl")
    ly = shell.current_page()
    assert H.wait(qapp, lambda: ly.badge.text() != "—", timeout=40.0), "大厅没回填账号"
    assert H.wait(qapp, lambda: ly._games == [], timeout=20.0), "新账号不该有历史"

    # ---------------- 单条删除
    gid = _resign_a_new_game(shell, qapp)
    assert H.wait(qapp, lambda: [g["id"] for g in ly._games] == [gid], timeout=30.0), \
        f"回大厅后列表里没看到那一局：{ly._games}"
    # 认输即一负。这条要等 /api/auth/me 的回包落地再断言：列表与 stats 是同一批
    # GET，机器负载高时 me 的回包可能晚窗几毫秒到达（全量跑 KataGo 支时现场红过一次）。
    assert H.wait(qapp, lambda: ly.statCards["record"].text() == "0 胜 / 1 负",
                  timeout=15.0), ly.statCards["record"].text()
    ly.games.setCurrentRow(0)
    assert ly.btnDelSel.isEnabled(), "选中了一条却不许删"
    assert not ly.btnClear.isVisible() or ly.btnClear.isEnabled()
    QTest.mouseClick(ly.btnDelSel, Qt.LeftButton)
    assert ly.btnDelOk.isVisible() and not ly.btnDelSel.isVisible(), \
        "删除没有二次确认（与网页版同形：点一下变成「确认删除 / 取消」）"
    # 六个控件挤进「最近对局」那一行是个有决定的摆法（大厅只剩 15px 高度余量），
    # 而 Qt 装不下时不报错不警告、只把文字裁一截 —— 只能当场量。
    offenders, _ = H.clipped_texts(ly)
    assert not offenders, "删除的确认态把那一行挤破了：" + "；".join(offenders)
    H.snap(shell, "page_04_lobby_del_confirm")   # 这个新排版得有人看过一眼
    QTest.mouseClick(ly.btnDelOk, Qt.LeftButton)
    assert H.wait(qapp, lambda: ly._games == [], timeout=30.0), "删了列表还在"
    note = ly.notice.text()
    assert "战绩已保留" in note and "0 胜 1 负" in note and "0.0%" in note, \
        f"删完没把战绩原样念回来：{note!r}"
    assert ly.statCards["record"].text() == "0 胜 / 1 负", "删记录把战绩改掉了"
    assert ly.statCards["winrate"].text() == "0%", ly.statCards["winrate"].text()
    assert not ly.btnDelOk.isVisible(), "确认态没复位"
    assert not ly.btnDelSel.isEnabled(), "没得删了却还亮着"

    # ---------------- 清空历史（另一个端点，走同一套二次确认与文案）
    _resign_a_new_game(shell, qapp)
    assert H.wait(qapp, lambda: len(ly._games) == 1, timeout=30.0), "第二局没进列表"
    assert ly.statCards["record"].text() == "0 胜 / 2 负"
    QTest.mouseClick(ly.btnClear, Qt.LeftButton)
    assert ly.btnClearOk.isVisible() and not ly.btnClear.isVisible(), "清空也没有二次确认"
    # 清空那一态的说明比删除那态更长，而且右边还顶着一排控件 —— 单独量一遍
    offenders, _ = H.clipped_texts(ly)
    assert not offenders, "清空的确认态把那一行挤破了：" + "；".join(offenders)
    QTest.mouseClick(ly.btnClearOk, Qt.LeftButton)
    assert H.wait(qapp, lambda: ly._games == [], timeout=30.0), "清空没生效"
    assert ly.statCards["record"].text() == "0 胜 / 2 负", "清空把战绩改掉了"
    assert "战绩已保留" in ly.notice.text() and "2 负" in ly.notice.text(), ly.notice.text()


def test_the_delete_confirm_cancels_without_touching_anything(shell, qapp):
    """二次确认的「取消」必须真的什么都不发（用户点错是常事）。

    与对局页的认输/强制结束同一口径。只验 UI：点取消后列表还得是满的。
    """
    _register(shell, qapp, prefix="cn")
    ly = shell.current_page()
    assert H.wait(qapp, lambda: ly.badge.text() != "—", timeout=40.0)
    _resign_a_new_game(shell, qapp)
    assert H.wait(qapp, lambda: len(ly._games) == 1, timeout=30.0)
    ly.games.setCurrentRow(0)
    QTest.mouseClick(ly.btnDelSel, Qt.LeftButton)
    assert ly.btnDelOk.isVisible()
    QTest.mouseClick(ly.btnDelCancel, Qt.LeftButton)
    qapp.processEvents()
    assert ly.btnDelSel.isVisible() and not ly.btnDelOk.isVisible(), "取消没回到原样"
    assert len(ly._games) == 1, f"只点取消却把记录删了：{ly._games}"
    assert ly.games.currentRow() == 0, "取消把选中项也弄丢了"


# ---------------------------------------------------------------- 导航栏

def test_the_nav_rail_has_no_brand_label(shell, qapp):
    """导航栏顶部不放品牌字（用户 2026-09-08 点名删掉「围棋教学」）。

    窗口标题已经是「弈道」，这一行既重复又俗；删掉后段位徽章直接顶到最上面，
    顺带把纵向空间还给导航按钮。
    """
    _register(shell, qapp, prefix="nb")
    rail = shell.findChild(QWidget, "navRail")
    assert rail is not None, "找不到导航栏"
    texts = [lab.text() for lab in rail.findChildren(QLabel) if lab.text().strip()]
    assert "围棋教学" not in texts, f"品牌字又回来了：{texts}"
    assert texts and texts[0] == shell.rankBadge.text(), \
        f"导航栏第一个标签应当是段位徽章，实际是：{texts[:2]}"


# ---------------------------------------------------------------- 记录 → 复盘

def _fake_records(ly, qapp):
    """塞两条假记录：一条进行中、一条已终局。只验「点得开哪一页」，不打后端。"""
    ly._games = [{"id": "g-ongoing", "finished": False, "rankName": "12级", "size": 19,
                  "resultText": "", "reviewStatus": "none"},
                 {"id": "g-done", "finished": True, "rankName": "12级", "size": 19,
                  "resultText": "黑中盘胜", "reviewStatus": "done"}]
    ly._paint_games()
    qapp.processEvents()


def test_the_review_button_opens_a_finished_record(shell, qapp):
    """用户报的「点对局记录看不到复盘」：现在选中一条已终局的局，点「查看复盘」直接进复盘页。

    之前记录只有一条路 —— 双击 → **对局页**，而且单击完全没反应。
    """
    _register(shell, qapp, prefix="rv")
    ly = shell.current_page()
    assert H.wait(qapp, lambda: ly.badge.text() != "—", timeout=40.0)
    _fake_records(ly, qapp)

    ly.games.setCurrentRow(0)                     # 进行中：没有复盘可看
    assert not ly.btnReview.isEnabled(), "进行中的局不该给「查看复盘」"
    ly.games.setCurrentRow(1)                     # 已终局
    assert ly.btnReview.isEnabled(), "已终局的局点不开复盘"
    QTest.mouseClick(ly.btnReview, Qt.LeftButton)
    assert H.wait(qapp, lambda: type(shell.current_page()).__name__ == "ReviewPage",
                  timeout=20.0), f"没进复盘页，停在 {type(shell.current_page()).__name__}"
    assert shell.current_page().game_id == "g-done", shell.current_page().game_id


def test_double_click_goes_to_review_for_finished_and_to_board_for_ongoing(shell, qapp):
    """双击的落点按「这一局有没有终局」分岔：终局 → 复盘，进行中 → 棋盘。"""
    _register(shell, qapp, prefix="dc")
    ly = shell.current_page()
    assert H.wait(qapp, lambda: ly.badge.text() != "—", timeout=40.0)
    _fake_records(ly, qapp)

    seen: list[tuple[str, str]] = []
    ly.openRequested.connect(lambda gid: seen.append(("open", gid)))
    ly.reviewRequested.connect(lambda gid: seen.append(("review", gid)))

    ly._on_pick(ly.games.item(0))                 # 进行中
    ly._on_pick(ly.games.item(1))                 # 已终局
    assert seen == [("open", "g-ongoing"), ("review", "g-done")], seen


def test_the_review_button_does_not_break_the_lobby_row(shell, qapp):
    """多一颗按钮之后，「最近对局」那一行仍不许把文字挤裁（大厅只剩 15px 高度余量）。"""
    _register(shell, qapp, prefix="rw")
    ly = shell.current_page()
    assert H.wait(qapp, lambda: ly.badge.text() != "—", timeout=40.0)
    _fake_records(ly, qapp)
    ly.games.setCurrentRow(1)
    offenders, _ = H.clipped_texts(ly)
    assert not offenders, "加了一颗按钮把那一行挤破了：" + "；".join(offenders)
    H.snap(shell, "page_10_lobby_review_button")
