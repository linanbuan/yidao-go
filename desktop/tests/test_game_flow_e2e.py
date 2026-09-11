"""P2 界面层验收：真点棋盘，从大厅开局一直下到终局面板。

计划对这一段的口径是"走完一整盘到终局结算"，所以这里**不走后门**：
  · 开局用大厅表单（点「对阵 AI」），不直接 POST /api/games；
  · 落子用 `QTest.mouseClick` 打在棋盘交叉点上，不直接调 `page.send`；
  · 死子、确认终局、悔棋、认输、强制结束、导出 SGF 全是点按钮。
只有"把局面摆成终局形状"这一步碰了服务端内存（`wsutil.set_up_endgame`），
理由写在那个大文档字符串里：启发式引擎不会主动虚手，真下满一盘 9 路要 80 多手。

拆成两局而不是一局，是因为实测过不来：
`set_up_endgame` 只改棋盘不改手顺，而启发式引擎是**按手顺重建棋盘**的 ——
前面真下过十手再去摆形状，引擎看到的局面与服务端不一致，会走出「AI 直接认输」
这种结束方式（第一版就是这么失败的）。所以：
  · 第一局：真下开局，验回显/思考态/倒计时/推荐点/悔棋/认输（关键帧 06、07）；
  · 第二局：一开局就摆成终局形状，真点填单官 → 结算 → 死子点改 → 确认 → 终局
    面板 + 段位徽章 + SGF（关键帧 08、09）。

四张关键帧落 artifacts/，我逐张读图。
"""
from __future__ import annotations

import uuid

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel

from core import api as A
from core import backend_host as bh
from core.settings import Prefs
from tests import harness as H
from tests import wsutil as U
from ui import shell as shell_mod
from ui.pages import game as game_page
from ui.pages import lobby
from ui.pages import review as review_page

PASSWORD = "mimashou123"
SIZE = 9
#: 60 秒：既真的在计时（限时 0 就不计，倒计时那块没人验），又不会因为在等
#: AI 回手而被自己超时判负。摆终局形状那一局干脆不限时：那里的时间都花在
#: 服务端内存里改棋盘上，跟被测的东西无关。
MOVE_SECONDS = 60
BLACK, WHITE = 1, 2
#: 从 shell.set_progress 抄出来比对徽章文本用的全角空格。写成转义而不是字面量，
#: 是为了躲开"往文件里写中文时被改字"这类静默损坏。
SEP = "\u3000"


@pytest.fixture(scope="module")
def host():
    h = bh.BackendHost()
    h.start(timeout=90.0)
    yield h
    h.stop()


@pytest.fixture
def shell(qapp, host, tmp_path):
    prefs = Prefs(str(tmp_path / "client.ini"))
    sh = shell_mod.Shell(host, prefs)
    sh.resize(1280, 800)
    sh.show()
    qapp.processEvents()
    yield sh
    sh.close()
    sh.deleteLater()


# ------------------------------------------------------------------ 真点操作

def _click(page, x: int, y: int, button=Qt.LeftButton) -> None:
    """在棋盘交叉点上真点一下。坐标由控件自己算，不写死像素。"""
    pos = page.boardView.center(x, y).toPoint()
    QTest.mouseClick(page.boardView, button, Qt.NoModifier, pos)


def _seen(root) -> list[str]:
    """界面上**看得见**的标签文字。隐藏面板里的文字不算数 —— 拿它断
    「屏上不该出现 X」会假红，拿它断「X 必须在」会假绿。"""
    return [w.text() for w in root.findChildren(QLabel)
            if w.isVisible() and w.text().strip()]


def _login(shell, qapp, prefix: str = "e2") -> str:
    username = prefix + uuid.uuid4().hex[:6]
    QTest.keyClicks(shell._login.user, username)
    QTest.keyClicks(shell._login.pw, PASSWORD)
    QTest.mouseClick(shell._login.btnRegister, Qt.LeftButton)
    assert H.wait(qapp, lambda: shell._stack.currentIndex() == 1, timeout=40.0), \
        f"注册没进主界面：{shell._login.error.text()!r}"
    return username


def _start(shell, qapp, size: int = SIZE, seconds: int = MOVE_SECONDS):
    """大厅表单开局。返回 (对局页, 开局前的导航栏徽章文本)。"""
    page = shell.current_page()
    assert isinstance(page, lobby.LobbyPage)
    assert H.wait(qapp, lambda: page.badge.text() != "—", timeout=40.0), \
        f"大厅没回填账号数据：{page.notice.text()!r}"
    if H.katago_requested():
        # 一局记下的 `engine` 是**创建那一刻**的 active（`manager.create` 里取
        # `active_engine`），而 KataGo 预热是后台任务：没等到就开局，整局都会
        # 名正言顺地跑在启发式引擎上 —— 那一支就只测到了“开关没生效”。
        got, err = H.wait_engine_active(
            f"{shell._host.base_url}/api/system/status", shell._prefs.token)
        assert got.get("active") == "katago", \
            f"开局前 KataGo 没预热完：{got} 等待期异常={err!r}"
    badge_before = shell.rankBadge.text()
    page.cbSize.setCurrentIndex(page.cbSize.findData(size))
    page.cbTime.setCurrentIndex(page.cbTime.findData(seconds))
    assert page.cbSize.currentData() == size and page.cbTime.currentData() == seconds
    QTest.mouseClick(page.btnStart, Qt.LeftButton)
    assert H.wait(qapp, lambda: isinstance(shell.current_page(), game_page.GamePage),
                  timeout=40.0), "点了「对阵 AI」没切到对局页"
    g = shell.current_page()
    assert H.wait(qapp, lambda: g.status == "open" and g.phase == "playing"
                  and g.size == size, timeout=60.0), \
        f"连上了但首帧 state 没落进来：status={g.status} phase={g.phase}"
    return g, badge_before


def _wait_my_turn(page, qapp, timeout: float = 60.0) -> None:
    assert H.wait(qapp, lambda: page.is_my_turn, timeout=timeout), \
        f"等不到轮到我：phase={page.phase} nextColor={page.next_color} err={page.error!r}"


def _play(page, qapp, candidates) -> tuple[int, int]:
    """轮到玩家时按候选点真下一手，等 AI 回一手。已落子的点跳过。"""
    _wait_my_turn(page, qapp)
    before = len(page.moves)
    for x, y in candidates:
        if page.board[y][x] != 0:
            continue
        _click(page, x, y)
        assert H.wait(qapp, lambda: len(page.moves) > before or bool(page.error),
                      timeout=20.0), "点下去没有任何回音（既没落子也没报错）"
        if page.error:
            page.clear_error()          # 这手被拒：换下一个候选，不算失败
            continue
        assert H.wait(qapp, lambda: len(page.moves) >= before + 2
                      or page.phase != "playing", timeout=90.0), \
            f"我落子了但 AI 没回：thinking={page.thinking} err={page.error!r}"
        return x, y
    raise AssertionError("候选点全被占掉了，换一组候选")


def _progress_of(shell, host) -> dict:
    """服务端账号里的段位进度 —— 徽章对不对，唯一的外部凭据就是它。"""
    me = A.http_json("GET", f"{host.base_url}/api/auth/me", None, shell._prefs.token) or {}
    return (me.get("user") or {}).get("progress") or {}


def _badge_should_say(shell, host) -> str:
    p = _progress_of(shell, host)
    return f"{p.get('rankName', '')}{SEP}{p.get('rankWins', 0)}/{p.get('winsRequired', 0)} 胜"


# ------------------------------------------------------------------ 第一局：开局到认输

def test_lobby_to_board_opening_countdown_takeback_resign(shell, qapp, host):
    """大厅开局 → 真点五手 → 倒计时 → 推荐点 → 悔棋 → 认输。关键帧 06、07。"""
    _login(shell, qapp)
    page, badge_before = _start(shell, qapp)
    sound = shell._sound

    # ---------------- 开局：连接、元信息、空盘
    assert page.size == SIZE and page.komi == 7.5
    assert page.boardView.board_size == SIZE, "首帧没把棋盘路数带过来"
    assert page.connBadge.text() == "已连接", page.connBadge.text()
    assert "18" in page.rankBadge.text(), f"新账号该从 18 级开始：{page.rankBadge.text()}"
    # 引擎看开关而不是写死：这一份 E2E 要带 `GO_KATAGO_ENABLED=true` 再跑一遍
    # （计划口径「两种都要跑」），写死启发式会在刚进对局的第一条引擎断言上就红。
    engine = (page.meta["engine"] or "").lower()
    if H.katago_requested():
        assert engine == "katago", \
            f"这一支要求整盘的真对手是 KataGo：{page.meta['engine']!r}"
    else:
        assert engine and "katago" not in engine, \
            f"这一支该走内置启发式引擎：{page.meta['engine']!r}"
    assert not page.emptyHint.isVisible()
    assert page.gauge["moves"].text() == "0"
    assert page.metaLine.text() and page.aiName.text() != "AI", \
        f"侧栏没写出对手是谁：{page.aiName.text()!r} / {page.metaLine.text()!r}"
    open_shot = H.snap(shell, "page_06_game_open")
    assert H.blank_ratio(shell) > 0.05, "开局截图几乎全空，等于没截到界面"
    # 文字被裁是看图才发现的（「虚手（pass）」画成「手（pass」）：按钮照样能点、
    # 信号照样发，行为断言拿它没办法，所以这里把「看图」机械化下来。
    offenders, scanned = H.clipped_texts(shell)
    assert scanned >= 10, f"只扫到 {scanned} 个带文字的控件，遍历八成没走通（假绿）"
    assert not offenders, "开局界面有文字被裁：" + "；".join(offenders)
    # 进度条也不能在 10px 的细槽里画字（QSS 关不掉，只能 `setTextVisible(False)`）。
    bar_bad, bar_n = H.bar_texts(shell)
    assert bar_n >= 1, "对局页连一个进度条都没扫到（复盘那条应该有）"
    assert not bar_bad, "；".join(bar_bad)

    # ---------------- 第一手：回显 / 思考态 / 落子音 / AI 回手 / 推荐点
    # 这一手不走 _play：要的就是"AI 已接单但还没算完"中间那一照。
    _wait_my_turn(page, qapp)
    sound.forget_played()
    flips: list[bool] = []
    page.thinkingChanged.connect(flips.append)
    x, y = 2, 6
    _click(page, x, y)
    assert H.wait(qapp, lambda: len(page.moves) >= 1, timeout=20.0), \
        f"点下去 20 秒没回显：error={page.error!r}"
    assert (page.moves[0]["x"], page.moves[0]["y"]) == (x, y), \
        f"回显的那手与我点的不一致：{page.moves[0]} vs ({x},{y})"
    # 思考态断的是「出现过」而不是「此刻是」：我这手的回显与 AI 的回手可能挤在
    # 同一次 processEvents 里送达，瞬时值会被 aiMove 当场改回 False（跟上面那个
    # flaky 同一类）。信号能记下每一次翻转，拿它断才不受调度时机影响。
    assert H.wait(qapp, lambda: True in flips, timeout=5.0), \
        f"落子后根本没进过思考态：thinking={page.thinking} flips={flips}"
    # 思考条该长什么样不靠时序拿：直接置位再走一次 WS 处理完统一走的那条 `_paint`，
    # 否则 AI 回手快时这一照就全凭运气。
    page.thinking = True
    page._paint()
    assert page.thinkBar.isVisible() and "思考" in page.thinkBar.label.text(), \
        page.thinkBar.label.text()
    assert sound.count_of("stone") == 1, f"我这手该响一声落子音：{sound.played}"
    assert page.board[y][x] == page.player_color
    assert H.wait(qapp, lambda: len(page.moves) >= 2, timeout=90.0), "AI 没回手"
    assert sound.count_of("stoneAi") == 1, f"AI 那手该响一声另一音色：{sound.played}"
    assert not page.thinking and page.phase == "playing"
    assert False in flips, "思考态只亮没灭：aiMove 那条路径没把它收掉"
    assert not page.thinkBar.isVisible(), "AI 回手了还挂着思考中就是假信息"
    assert H.wait(qapp, lambda: bool(page.hint), timeout=40.0), "分析没带回推荐点"
    assert page.hintBadges and "%" in page.hintBadges[0].text(), \
        [b.text() for b in page.hintBadges]
    assert "推荐" in page.hintCap.text()
    assert page.gauge["winrate"].text() != "—", "有了分析，胜率条还是空的"

    # ---------------- 倒计时：服务端给起点，本地只走显示
    assert page.move_seconds_left is not None, "AI 回手后该开始本手限时"
    assert page.move_seconds == MOVE_SECONDS
    assert page.clockBar.isVisible() and "剩余" in page.clockBar.label.text(), \
        page.clockBar.label.text()
    first = page.seconds_left()
    QTest.qWait(1600)
    second = page.seconds_left()
    assert second < first, f"本地没在走显示：{first} -> {second}"
    assert second >= MOVE_SECONDS - 8, f"数得比真实时间还快：{first} -> {second}"

    # ---------------- 再下四手，攒一个像样的中盘
    for _ in range(4):
        _play(page, qapp, [(6, 6), (2, 2), (4, 2), (2, 4), (7, 1), (1, 7), (5, 3), (3, 3)])
    assert len(page.moves) == 10
    assert page.gauge["moves"].text() == "10"
    assert page.error == ""
    mid_shot = H.snap(shell, "page_07_game_midgame")
    # 中盘图必须和开局图不一样：曾经出现过两张截图同一张而没人发现
    assert mid_shot.read_bytes() != open_shot.read_bytes()
    assert not H.clipped_texts(shell)[0], "中盘多出来的推荐点把侧栏撑破了"

    # ---------------- 回看一手再回当前（只改显示，不碰数据）
    page.set_view_ply(4)
    assert page.viewBar.isVisible() and "查看" in page.viewBar.label.text()
    assert not page.is_my_turn, "回看时不许落子"
    assert len(page.moves) == 10, "回看改动了手顺就是越界"
    QTest.mouseClick(page.viewBar.button, Qt.LeftButton)
    assert page.view_ply is None and not page.viewBar.isVisible()

    # ---------------- 悔棋两手：退回 8 手，盘上那两点清掉
    last_x, last_y = page.moves[-2]["x"], page.moves[-2]["y"]
    sound.forget_played()
    QTest.mouseClick(page.btnTakeback, Qt.LeftButton)
    assert H.wait(qapp, lambda: len(page.moves) == 8, timeout=40.0), \
        f"悔棋没退回两手：{len(page.moves)} 手，error={page.error!r}"
    assert page.error == ""
    assert page.board[last_y][last_x] == 0, "悔掉的那手在盘上还留着"
    assert sound.count_of("click") == 1
    assert page.btnTakeback.isEnabled(), "还有 8 手棋，悔棋按钮该还能点"
    assert page.gauge["moves"].text() == "8"

    # ---------------- 虚手：服务端认这条指令（网页版发的是被拒的那条）
    before = len(page.moves)
    _wait_my_turn(page, qapp)
    QTest.mouseClick(page.btnPass, Qt.LeftButton)
    assert H.wait(qapp, lambda: len(page.moves) > before or bool(page.error),
                  timeout=30.0), "点虚手没有任何回音"
    assert page.error == "", f"虚手被服务端拒了：{page.error!r}"
    assert page.moves[before]["gtp"] == "pass", page.moves[before]
    assert H.wait(qapp, lambda: len(page.moves) >= before + 2, timeout=90.0), "AI 没跟着回手"

    # ---------------- 认输：两次点击（要点确认），一负记进战绩
    sound.forget_played()
    QTest.mouseClick(page.btnResign, Qt.LeftButton)
    assert page.btnResignOk.isVisible() and not page.btnResign.isVisible(), \
        "点认输该先要一次确认，而不是一下就结束"
    QTest.mouseClick(page.btnResignCancel, Qt.LeftButton)
    assert page.btnResign.isVisible() and page.game_end is None, "取消该回到原样"
    QTest.mouseClick(page.btnResign, Qt.LeftButton)
    QTest.mouseClick(page.btnResignOk, Qt.LeftButton)
    assert H.wait(qapp, lambda: page.game_end is not None, timeout=40.0), \
        f"确认认输没等到 gameEnd：error={page.error!r}"
    end = page.game_end
    assert end["reason"] == "player-resign" and end["playerWon"] is False
    assert page.phase == "finished" and page.endPanel.isVisible()
    assert page.endTitle.text() != "你赢了！"
    assert not page.opsPanel.isVisible(), "终局了还留着对局操作面板"
    assert sound.count_of("lose") == 1, sound.played
    # 输棋不动胜场徽章，但必须与服务端一致 —— "变了"不是正确，"一致"才是。
    assert shell.rankBadge.text() == _badge_should_say(shell, host), shell.rankBadge.text()
    assert _progress_of(shell, host).get("rankLosses") == 1, \
        f"认输没记进战绩：{_progress_of(shell, host)}"
    assert page.btnReview.isVisible()
    page.shutdown()


# ------------------------------------------------------------------ 第二局：终局结算

def test_fill_dame_scoring_final_panel_and_sgf(shell, qapp, host, tmp_path):
    """摆成终局形状 → 真点填单官 → 结算 → 点改死子 → 确认 → 终局面板 → 导出 SGF。"""
    _login(shell, qapp, prefix="sc")
    page, badge_before = _start(shell, qapp, seconds=0)
    sound = shell._sound
    assert page.game_id

    # ---------------- 摆形状后用页面自己的重连路径同步回来
    shape = U.set_up_endgame(page.game_id, SIZE)
    mid = shape["mid"]
    page.shutdown()
    page.open_game(page.game_id)
    assert H.wait(qapp, lambda: page.status == "open"
                  and page.board[0][mid - 1] == BLACK
                  and page.board[0][SIZE - 1] == WHITE, timeout=60.0), \
        "重连后的全量 state 没把摆好的形状同步过来"
    assert page.board[mid][mid] == 0, "中央那一列该是单官"
    assert page.phase == "playing" and page.moves == []

    # ---------------- 真点填单官，直到双虚手进结算
    rounds = 0
    while page.phase == "playing" and rounds < 30:
        rounds += 1
        # 等的是「轮到我 **或** 已经离开对局阶段」，不是死等 `is_my_turn`。
        # AI 也会自己虚手（单官填完它无子可下），双虚手当场进结算 —— 那是合法
        # 路径。循环顶部判的 phase 到下面这次等待之间正是它发生的窗口，只等轮到
        # 自己就会在这儿干耗 60 秒再超时（实测约 1/7 的 run 会撞上）。
        assert H.wait(qapp, lambda: page.is_my_turn or page.phase != "playing",
                      timeout=60.0), \
            f"等不到轮到我：phase={page.phase} nextColor={page.next_color} err={page.error!r}"
        if page.phase != "playing":
            break                     # 已被 AI 的虚手推进到结算，不用再等自己
        empties = [(mid, yy) for yy in range(SIZE) if page.board[yy][mid] == 0]
        if empties:
            _play(page, qapp, empties)
            continue
        # 单官填完了就真的没子可下：这时只能 pass。刻意不把对方眼位当候选 ——
        # 往眼里下子是自杀，服务端会拒，而一次被拒的落子测不到"填完单官"这件事。
        before = len(page.moves)
        QTest.mouseClick(page.btnPass, Qt.LeftButton)
        assert H.wait(qapp, lambda: len(page.moves) > before
                      or page.phase != "playing", timeout=90.0), \
            f"pass 没推动局面：error={page.error!r}"
    assert page.phase == "scoring", \
        f"填完单官没进结算：phase={page.phase} err={page.error!r}"
    assert page.scoringPanel.isVisible()
    assert page.scoreBar.isVisible() and page.scoreBar.label.text()
    assert "枚" in page.scoreRows["dead"].text()
    assert page.scoreRows["black"].text() != "—"
    assert not page.thinkBar.isVisible(), "结算阶段不该显示思考中"
    assert not page.clockBar.isVisible(), "结算阶段不该计时"

    # ---------------- 死子可点改：点一下改判、再点一下回到原样
    # 引擎**估不估得出死子不作为前提**：`set_up_endgame` 摆的是两条各留两个
    # 真眼的活棋，KataGo 判「没有死子」是对的，内置启发式会报一大把 ——
    # 两个答案都合法，而旧版这里写的是 `assert page.dead_stones`，
    # 于是 KataGo 支隔几次就红一次（实测：`assert []`）。
    # 同一个坑在 `test_ws_flow.py` 里已经填过一次（那里 L348 的注释），UI 层
    # 这一处是它的复制品 —— 把前提建在一家引擎的输出上，两支里必然有一支不对。
    # 这条用例真正要验的是「点棋盘改判死 → 面板计数跟着变」这条通路，
    # 所以两个方向都点一遍：已判死的点它该减一，活着的点它该加一。
    if page.dead_stones:
        px, py = int(page.dead_stones[0][0]), int(page.dead_stones[0][1])
        was_dead = True
    else:
        picked = next(((xx, yy) for yy in range(SIZE) for xx in range(SIZE)
                       if page.board[yy][xx] == WHITE), None)
        assert picked, "盘上一颗白子都没有，这个形状摆坏了"
        px, py = picked
        was_dead = False
    before = len(page.dead_stones)
    _click(page, px, py)
    assert len(page.dead_stones) == before + (-1 if was_dead else 1), \
        f"点一颗{'已判死' if was_dead else '活着'}的子，计数该{'减' if was_dead else '加'} 1"
    assert page.scoreRows["dead"].text().startswith(f"{len(page.dead_stones)} 枚"), \
        f"面板计数没跟着改：{page.scoreRows['dead'].text()!r} / 实际 {len(page.dead_stones)} 枚"
    _click(page, px, py)
    assert len(page.dead_stones) == before, "再点一下该回到原样"
    assert page.error == ""
    scoring_shot = H.snap(shell, "page_08_game_scoring")
    assert not H.clipped_texts(shell)[0], "结算面板里有文字被裁（那一栏文字最长）"

    # ---------------- 确认终局：判死全部白子，赢下这一局
    # 不逐枚点 30 多下：要的结果是"这一局我赢"，而逐个真点击不会多提供任何信息
    # （点击通路在上面那两下里已经验过了）。
    page.dead_stones = [[xx, yy] for yy in range(SIZE) for xx in range(SIZE)
                        if page.board[yy][xx] == WHITE]
    page._paint()                       # 私有方法：只为让标记真的画进截图
    sound.forget_played()
    QTest.mouseClick(page.btnScoreOk, Qt.LeftButton)
    assert H.wait(qapp, lambda: page.game_end is not None, timeout=40.0), \
        f"确认终局没等到 gameEnd：error={page.error!r}"

    end = page.game_end
    assert end["reason"] == "pass-pass"
    assert end["playerWon"] is True and end["result"]["winner"] == BLACK
    assert end["result"]["whiteStones"] == 0
    assert page.phase == "finished"
    assert page.endPanel.isVisible() and page.endTitle.text() == "你赢了！"
    assert page.endAlert.label.text(), "结果文案是空的"
    assert not page.scoringPanel.isVisible()
    assert sound.count_of("win") == 1, f"赢了该响胜负音：{sound.played}"
    assert page.gauge["moves"].text() == str(len(page.moves))

    # ---------------- 段位徽章：与服务端一致，且因为这一胜而变了
    prog = end["rank"]["progress"]
    assert prog["rankName"]
    srv = _progress_of(shell, host)
    assert (srv.get("rankName"), srv.get("rankWins")) == (prog["rankName"], prog["rankWins"]), \
        f"事件里的段位进度与服务端不符：{prog} vs {srv}"
    want = f"{prog['rankName']}{SEP}{prog['rankWins']}/{prog['winsRequired']} 胜"
    assert shell.rankBadge.isVisible()
    assert shell.rankBadge.text() == want, f"导航栏徽章：{shell.rankBadge.text()!r} != {want!r}"
    assert shell.rankBadge.text() != badge_before, f"赢了却没动徽章（开局时 {badge_before!r}）"
    assert prog["rankWins"] == 1, prog

    end_shot = H.snap(shell, "page_09_game_end")
    for a, b in ((scoring_shot, end_shot),):
        assert a.read_bytes() != b.read_bytes(), f"{a.name} 与 {b.name} 是同一张图"

    # 终局之后死子的叉还留在盘上（原生端有意偏离网页版的一处，理由在 `_paint_board`）：
    # 结论文字说「白子全死」，盘上却画成一局白子活着的棋，就是图文不符。
    # 断的是**控件**那份（画出来的），不是页面那份（待确认的）—— 用户看得见的是前者。
    assert page.boardView.shown_dead, "终局面板出来了却把死子标记清空了"
    assert len(page.boardView.shown_dead) == len(end["result"]["deadStones"])
    page.set_view_ply(1)                    # 回看旧局面时不许叠终局判定
    assert not page.boardView.shown_dead, "回看时还把终局的死子叉画在旧局面上"
    page.set_view_ply(None)
    assert page.boardView.shown_dead, "回到当前后死子标记该跟着回来"

    # ---------------- SGF 导出（原生端独有：网页版没有下载口）
    sgf = tmp_path / "final.sgf"
    assert page.export_sgf_to(str(sgf))
    assert H.wait(qapp, lambda: sgf.exists() and sgf.read_text("utf-8").startswith("(;"),
                  timeout=40.0), f"SGF 没落到 {sgf}"
    text = sgf.read_text("utf-8")
    assert "SZ[9]" in text, text[:120]
    assert page.error == ""

    # ---------------- 返回大厅：对局页那个按钮真的把用户送回去（送完再回来，
    # 下面还要用这一局进复盘）。顺便验外壳是**缓存页面**而不是重建：
    # 重建会把刚跑完的那一局丢掉，而这是用户下一步要看的东西。
    QTest.mouseClick(page.btnLobby, Qt.LeftButton)
    assert H.wait(qapp, lambda: isinstance(shell.current_page(), lobby.LobbyPage),
                  timeout=20.0), "点「返回大厅」没回去"
    shell.go("game")
    qapp.processEvents()
    assert shell.current_page() is page, "切走再切回来把对局页重建了（那一局就丢了）"

    # ---------------- 复盘：计划对 P4 的验收现场 —— 拿刚刚这盘**真棋**出报告
    QTest.mouseClick(page.btnReview, Qt.LeftButton)
    assert H.wait(qapp, lambda: isinstance(shell.current_page(), review_page.ReviewPage),
                  timeout=20.0), "点「生成 AI 复盘」没切到复盘页"
    rp = shell.current_page()
    assert rp.game_id == page.game_id, f"复盘页接上的不是刚刚这一局：{rp.game_id}"
    # 终局时后端自动入队复盘，到这儿可能还在生成中。复盘页自己每 1.2 秒轮一次
    # 进度、完成后再取整份，这里只是陪它等（上限 4 分钟：引擎预热最坏要 3 分钟）。
    assert H.wait(qapp, lambda: rp.report is not None, timeout=240.0), \
        (f"等不到复盘报告：status={rp._status()!r} prog={rp.prog} "
         f"reviewError={rp.meta.get('reviewError')!r} error={rp.error!r}")
    qapp.processEvents()

    # 曲线点数。计划写的是「点数 == 手数」，按事实断成 **手数 + 1**：第 0 个点是
    # 「还没落子时引擎怎么看这盘」，删了它才是错（同一处口径见 test_review_flow）。
    assert len(rp.moves) == len(page.moves), "复盘页的手顺比对局页少了一截"
    curve = rp._curve()
    assert len(curve) == len(rp.moves) + 1, \
        f"真报告里曲线点数对不上手数：{len(curve)} vs {len(rp.moves)}+1"
    assert rp.chart.plotted_points() == len(curve), "控件静默丢了点"
    assert not rp.genPanel.isVisible(), "报告都到手了还挂着生成中"
    assert not rp.noneBar.isVisible()
    assert rp.error == "", rp.error

    # 目差那一列读数必须真的画得出来。QtCharts 在标签带装不下时不报错也不警告，
    # 只把每个数字缩成「···」—— 一整条轴的读数就这么静默丢了。
    # 来路：读旧版留下的那张 `page_10_review_real.png` 时看见右轴全是一列点，
    # 真报告重跑一遍才量出成因：`nice_score_range` 按外扩**之前**的跨度选档，
    # 外扩完多出的两格没人重数，-14~-4 那一组拿到 8 个刻度（上限 6），
    # 8 行字挤 82px 高的标签带就全被省略号化。同一个形状在
    # `test_winrate_chart.py::test_a_wide_score_range_still_prints_numbers` 里钉了一份快的。
    # 阈值口径（实测）：真报告上这一列有 58 个墨像素（四枚「-14」这样的标签，
    # 一枚 ~15）；全被省略号化之后整列只剩 9 个。
    ax = rp.chart._axis_score
    pa = rp.chart.chart().plotArea()
    ink = H.chart_axis_ink(rp.chart)
    _lo, _hi, _n = ax.min(), ax.max(), ax.tickCount()
    _labels = [ax.labelFormat() % (_lo + i * (_hi - _lo) / max(1, _n - 1))
               for i in range(_n)]
    assert ink >= 40, (
        f"目差轴标签带只有 {ink} 个墨像素（真报告实测 58，全成「···」约 30）："
        f"读数被省略号化了。轴 {_lo}~{_hi} ticks={_n} fmt={ax.labelFormat()} "
        f"标签={_labels} 标题={ax.titleText()!r} 控件 "
        f"{rp.chart.width()}x{rp.chart.height()} 绘图区 {pa.width():.0f}x{pa.height():.0f} "
        f"右带 {rp.chart.width() - pa.right():.0f}px 紧凑={rp.chart.compact}")

    # 逐一手过一遍**渲染之后**的界面：计划那两条口径（不带 `**`、「离你这手」
    # 不许指到 AI 的手）在真报告上落地。只走带讲解的那几手：没讲解的手上
    # 卡片只有数字，扫它扫不出什么。
    commented = [m for m in rp.report["moves"] if m.get("comment")]
    for m in commented:
        rp.set_ply(int(m["ply"]))
        qapp.processEvents()                         # 不转就是只读到旧卡片
        text = " ".join(_seen(rp.cardPanel))
        assert text, f"第 {m['ply']} 手有讲解却什么都没画出来"
        assert "**" not in text and "`" not in text, \
            f"第 {m['ply']} 手的讲解带着 Markdown 记号：{text[:160]}"
        if not m.get("isPlayer"):
            assert "离你这手" not in text, \
                f"AI 的第 {m['ply']} 手用「你这手」称呼玩家：{text[:160]}"
    # 防「空循环也是绿」：一处讲解都没有，只能是因为这一局真的没有问题手
    assert commented or all(m["flag"] in ("good", "pass") for m in rp.report["moves"]), \
        "有被评级为问题手的一手却没生成任何讲解：这条路在这盘上断了"

    whole = " ".join(_seen(rp))
    assert "**" not in whole and "`" not in whole, "整页里扫到了 Markdown 记号"
    assert not H.clipped_texts(shell)[0], "复盘这一屏有文字被裁"
    review_shot = H.snap(shell, "page_10_review_real")
    assert review_shot.exists()
    rp.shutdown()

    # ---------------- 复盘页自己的「返回大厅」（与对局页那个不是同一个按钮）
    QTest.mouseClick(rp.btnBack, Qt.LeftButton)
    assert H.wait(qapp, lambda: isinstance(shell.current_page(), lobby.LobbyPage),
                  timeout=20.0), "复盘页点「返回大厅」没回去"
    page.shutdown()


# ------------------------------------------------------------------ 作废局

def test_force_end_voids_the_game_and_freezes_the_badge(shell, qapp, host):
    """强制结束：走 REST，但界面必须靠同一条 WS 事件更新，徽章不许动。"""
    _login(shell, qapp, prefix="fe")
    page, badge_before = _start(shell, qapp, seconds=0)
    _play(page, qapp, [(4, 4), (2, 6)])
    sound = shell._sound
    sound.forget_played()

    QTest.mouseClick(page.btnForce, Qt.LeftButton)
    assert page.btnForceOk.isVisible() and page.forceNote.isVisible()
    assert page.forceNote.text() == "这一局作废，不影响战绩与晋升进度。"
    QTest.mouseClick(page.btnForceOk, Qt.LeftButton)
    assert H.wait(qapp, lambda: page.game_end is not None, timeout=40.0), \
        f"强制结束没落到界面上：error={page.error!r}"
    end = page.game_end
    assert end["reason"] == "force-end" and end["countsForRank"] is False
    assert end.get("rank") is None, "作废局不该带段位变动"
    assert page.endTitle.text() == "对局已作废", page.endTitle.text()
    assert page.endAlert.kind == "warn", "作废要用警告色，不是绿底"
    assert shell.rankBadge.text() == badge_before, "作废局把徽章改了就是假战绩"
    assert shell.rankBadge.text() == _badge_should_say(shell, host)
    assert _progress_of(shell, host).get("totalGames") == 0, "作废局记进了总场次"
    assert sound.count_of("win") == 0 and sound.count_of("lose") == 0
    # 先多转一秒再数：服务端一边从 WS 推、一边把同一个 dict 塞进 REST 响应，
    # 同一局 gameEnd 就是会到两次。上一轮这里偶发红（两声 click）而单跑是绿的，
    # 差的不是代码是断言时机 —— 不先给第二份送达留时间，“只响一声”这条断言就是在赌时序。
    # （幂等本身在 `test_pages.test_a_game_end_delivered_twice_still_announces_once`
    # 里有一个必然成立的钉法，不依赖这里的时序。）
    H.settle(qapp, 1.0)
    assert sound.count_of("click") == 1, f"作废只该响一声点击：{sound.played}"
    page.shutdown()


# ------------------------------------------------------------------ 错误与恢复

def test_illegal_click_shows_the_error_bar_and_keeps_the_socket(shell, qapp):
    """同一点连下两手：第二手被服务端拒，横幅报错、轮次不变、连接还活着。"""
    _login(shell, qapp, prefix="il")
    page, _badge = _start(shell, qapp, seconds=0)
    x, y = _play(page, qapp, [(4, 4), (2, 2)])
    before = len(page.moves)
    shell._sound.forget_played()
    _click(page, x, y)
    assert H.wait(qapp, lambda: bool(page.error), timeout=30.0), "点已有子的点没报错"
    assert page.errorBar.isVisible() and page.errorBar.label.text()
    assert shell._sound.count_of("wrong") == 1
    assert len(page.moves) == before, "被拒的一手不该进手顺"
    assert page.status == "open", "一次手滑不能把连接搞没"
    QTest.mouseClick(page.errorBar.button, Qt.LeftButton)      # 「知道了」
    assert not page.errorBar.isVisible() and page.error == ""
    _play(page, qapp, [(2, 6), (6, 2), (7, 7)])
    assert len(page.moves) == before + 2
    page.shutdown()


def test_reconnect_recovers_the_board_without_a_reload(shell, qapp):
    """断线自动重连：回到 open 状态后仍拿到一份全量 state，界面不用刷新。"""
    _login(shell, qapp, prefix="rc")
    page, _badge = _start(shell, qapp, seconds=0)
    x, y = _play(page, qapp, [(4, 4), (2, 6)])
    assert page.board[y][x] == page.player_color
    # 先把本地那一手抹掉：只有重连带回来的全量 state 能把它变回去，
    # 不然“重连恢复了局面”这句断言是恒真的（本地从来没丢过东西）。
    stale = [list(r) for r in page.board]
    stale[y][x] = 0
    page.board = stale
    assert page.board[y][x] == 0
    page._socket._ws.close()         # 模拟掉线：不是用户主动关的，所以该自动重连
    assert H.wait(qapp, lambda: page.status == "reconnecting", timeout=10.0), page.status
    assert H.wait(qapp, lambda: page.status == "open", timeout=60.0), \
        f"没自动重连上：last_error={page._socket.last_error!r}"
    assert H.wait(qapp, lambda: page.board[y][x] == page.player_color
                  and len(page.moves) >= 2, timeout=30.0), "重连后局面没从全量 state 恢复"
    assert page.connBadge.text() == "已连接"
    _play(page, qapp, [(6, 2), (7, 7), (2, 2)])
    page.shutdown()
