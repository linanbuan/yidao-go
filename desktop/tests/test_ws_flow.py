"""P2 协议层：真后端 + 真 QWebSocket 的事件往返。

跑真后端而不是 mock，理由与 `test_api_contract.py` 一样：网页版已冻结，桌面端与
后端之间唯一的"共同语言文档"就是这里的往返本身。用 mock 只会把我对字段的猜测
固化成测试 —— 那种测试全绿而界面白板。

刻意钉住的行为（都有过血的教训或对价码的影响）：
  · 玩家那手**立即回显**，`thinking` 在分析之前到（手感：点完不等 AI 想完）；
  · 非法点/未知指令只回 `error`，连接不死、轮次不变（一次手滑不能把对局搞没）；
  · 悔棋带回**整份 state**，客户端不自己推算回滚后的棋盘；
  · `moveSecondsLeft` 由服务端给（本地只走显示），超时以服务端时钟为准；
  · 断线自动重连后仍能从全量 state 恢复，不必自己补差；
  · `pong` 不透给页面；解析不了的帧要留痕迹（`bad_frames`）。
"""
from __future__ import annotations

import json

import pytest

from core import api as A
from core import backend_host as bh
from core import ws as W
from tests import harness as H
from tests import wsutil as U


@pytest.fixture(scope="module")
def host():
    h = bh.BackendHost()
    h.start(timeout=90.0)
    yield h
    h.stop()


@pytest.fixture
def game(host, qapp):
    """一个新账号 + 一局 9 路 + 已连上并收到首帧 state。"""
    user = U.register(host, tag="ws")
    res = U.create_game(host, user["token"])
    gid = res["game"]["id"]
    sk, rec = U.connect(host, user["token"], gid)
    assert H.wait(qapp, lambda: rec.first("state") is not None, timeout=30.0), \
        f"连上了但没收到首帧 state：statuses={rec.statuses} err={sk.last_error}"
    yield {"id": gid, "user": user, "socket": sk, "rec": rec, "created": res,
           "host": host}
    sk.close()          # 不关掉它会带着自动重连活到后面的用例里去


# ------------------------------------------------------------------ 纯函数与记账

def test_ws_url_variants():
    assert W.ws_url("http://127.0.0.1:8", "/ws/game/g1", "tk") == \
        "ws://127.0.0.1:8/ws/game/g1?token=tk"
    assert W.ws_url("https://example.com/", "/ws/game/g1", "") == \
        "wss://example.com/ws/game/g1?token="
    # 已经有 query 时用 &，不能塞出第二个 ?
    assert W.ws_url("http://h:1", "/ws/game/g1?x=2", "t") == \
        "ws://h:1/ws/game/g1?x=2&token=t"
    # 令牌里出现 & 必须被编掉，否则会把 query 切断
    assert W.ws_url("http://h:1", "/ws/g", "a&b=c") == "ws://h:1/ws/g?token=a%26b%3Dc"


class _RecordingSocket(W.GameSocket):
    """不起真连接，只看排队/补发/丢弃的账。`_raw_send` 是唯一碰网络的地方。"""

    def __init__(self):
        super().__init__(lambda: "ws://unused/ws")
        self.sent: list[dict] = []

    def _raw_send(self, msg):
        self.sent.append(msg)


def test_commands_are_queued_before_open_and_flushed_after():
    sk = _RecordingSocket()
    # 没 connect 之前 send 必须丢弃而不是攒着：页面已经切走了还攒指令，
    # 下一局会收到上一局的落子
    sk.send({"action": "resign"})
    assert sk.sent == [] and sk._pending == []

    sk._closed_by_user = False        # 相当于已经 call 过 connect_to_game 但还没 OPEN
    sk.send({"action": "move", "x": 4, "y": 4})
    assert sk.sent == [] and len(sk._pending) == 1

    for i in range(60):               # 队列必须有上限：断网期间无限堆积没有意义
        sk.send({"action": "ping", "n": i})
    assert len(sk._pending) == W.GameSocket.PENDING_LIMIT

    sk._on_open()
    assert sk.sent[0] == {"action": "move", "x": 4, "y": 4}, "补发必须按序"
    assert sk._pending == []
    assert sk._retries == 0 and sk.last_error == ""

    sk.close()
    assert sk.status == "closed"
    sk._pending.append({"action": "hint"})    # 假设断线前已攒进两条
    sk.send({"action": "pass"})               # 用户已经走了：队列必须清掉，不能留给下一局
    assert sk._pending == []


class _Sink:
    """绑定方法当槽：局部函数/lambda 会被静默丢投递（见 core.api.Reply 文档）。"""

    def __init__(self, fn):
        self._fn = fn

    def on_event(self, ev):
        self._fn(ev)


def test_unparsable_frames_are_counted_not_raised():
    sk = _RecordingSocket()
    sk._on_text("这根本不是 JSON")
    sk._on_text("[1, 2, 3]")
    assert sk.bad_frames == 2
    assert sk.sent == []


def test_pong_is_counted_but_not_forwarded(qapp):
    sk = _RecordingSocket()
    got = []
    sink = _Sink(got.append)
    sk.event.connect(sink.on_event)
    sk._on_text(json.dumps({"type": "pong"}))
    sk._on_text(json.dumps({"type": "thinking", "aiName": "小林一角"}, ensure_ascii=False))
    assert sk.pong_count == 1
    assert [e["type"] for e in got] == ["thinking"]


# ------------------------------------------------------------------ 首帧与落子往返

def test_first_frame_is_a_full_state(game):
    st = game["rec"].state
    for key in ("id", "size", "board", "moves", "analyses", "curve", "phase",
                "nextColor", "playerColor", "rankName", "aiName", "engine",
                "allowTakeback", "hintMode", "moveSeconds", "moveSecondsLeft",
                "scoringDead", "despairPlies", "profile", "komi", "handicap"):
        assert key in st, f"首帧少了 {key}，侧栏/棋盘会有一块永远空着"
    assert st["id"] == game["id"]
    assert st["phase"] == "playing"
    assert len(st["board"]) == 9 and all(len(r) == 9 for r in st["board"])
    assert st["nextColor"] == st["playerColor"], "新开且玩家执黑，第一手该玩家走"
    assert game["rec"].statuses[0] == "connecting" and "open" in game["rec"].statuses


def test_turning_recommendations_off_reaches_the_running_game(game, qapp):
    """设置页关掉「落子推荐」→ 进行中的对局当场收到 `hintMode` 事件。

    用户报过「关了推荐点还一直显示、再开再关一次才消失」的两半现场：
    一半在客户端（`show_hint` 随新局重置，见 test_pages），一半在服务端 ——
    活对局的 `hint_mode` 是开局时的快照，PATCH /api/auth/me 只落库不推过去，
    就得等下一局才生效。这里钉的就是服务端 → 客户端这条广播链路。
    """
    host = game["host"]
    token = game["user"]["token"]
    assert game["created"]["game"]["hintMode"] is True, "新账号默认开着推荐点"

    ok = A.http_json("PATCH", f"{host.base_url}/api/auth/me",
                     {"hintMode": False}, token)
    assert ok and ok.get("user", {}).get("hintMode") is False, ok
    done = H.wait(qapp, lambda: game["rec"].first("hintMode") is not None, timeout=10.0)
    assert done, f"进行中的对局没收到 hintMode 事件：{game['rec'].types}"
    assert game["rec"].first("hintMode") == {"type": "hintMode", "enabled": False}, \
        game["rec"].first("hintMode")

    # 再开回来：同一局当场恢复，不是下次开局才恢复
    ok = A.http_json("PATCH", f"{host.base_url}/api/auth/me",
                     {"hintMode": True}, token)
    assert ok, ok
    done = H.wait(qapp, lambda: len([e for e in game["rec"].events
                                     if e.get("type") == "hintMode"]) >= 2, timeout=10.0)
    assert done, f"第二次 hintMode 事件没到：{game['rec'].types}"
    hits = [e for e in game["rec"].events if e.get("type") == "hintMode"]
    assert len(hits) == 2 and hits[-1]["enabled"] is True, hits


def test_player_move_echoes_before_the_ai_answers(game, qapp):
    rec, sk = game["rec"], game["socket"]
    rec.clear()
    sk.send({"action": "move", "x": 4, "y": 4})
    assert H.wait(qapp, lambda: rec.first("aiMove") is not None, timeout=30.0), rec.types

    types = rec.types
    assert types[0] == "move", f"玩家那手要立即回显，实际顺序：{types}"
    assert "thinking" in types and types.index("thinking") < types.index("aiMove"), \
        "thinking 必须早于 aiMove，否则 AI 那 0.4 秒界面是白的"
    mv = rec.first("move")["move"]
    assert (mv["x"], mv["y"], mv["color"]) == (4, 4, 1)
    assert rec.first("move")["moveCount"] == 1
    ai = rec.first("aiMove")
    assert ai["move"]["color"] == 2 and ai["moveCount"] == 2
    an = rec.first("analysis")
    assert an is not None and an["index"] == 1, "分析的是玩家那手之后的局面（= AI 行棋局面）"
    assert an["analysis"], "analysis 事件得带上分析点本体"
    assert rec.types.count("aiMove") == 1, "AI 只能应一手"


def test_bad_commands_are_refused_without_killing_the_socket(game, qapp):
    rec, sk = game["rec"], game["socket"]
    # 1) 点在已有子的地方：先合法落一手，再点同一点
    sk.send({"action": "move", "x": 2, "y": 2})
    assert H.wait(qapp, lambda: rec.first("aiMove") is not None, timeout=30.0)
    rec.clear()
    sk.send({"action": "move", "x": 2, "y": 2})
    assert H.wait(qapp, lambda: rec.first("error") is not None, timeout=15.0), rec.types
    assert "move" not in rec.types, "非法点不该产生任何一手"

    # 2) 未知指令：文案要带指令名，不然排查协议漂移时看不出是谁发错了
    rec.clear()
    sk.send({"action": "teleport", "x": 1})
    assert H.wait(qapp, lambda: rec.first("error") is not None, timeout=15.0)
    assert "teleport" in rec.first("error")["message"]

    # 3) 没到结算就确认终局
    rec.clear()
    sk.send({"action": "scoreConfirm", "dead": []})
    assert H.wait(qapp, lambda: rec.first("error") is not None, timeout=15.0)
    assert "结算" in rec.first("error")["message"]

    assert sk.is_open, "三条坏指令之后连接必须还活着"
    rec.clear()
    sk.send({"action": "move", "x": 6, "y": 6})
    assert H.wait(qapp, lambda: rec.first("move") is not None, timeout=15.0), \
        "报错之后仍要能正常落子"


def test_takeback_returns_the_whole_state(game, qapp):
    rec, sk = game["rec"], game["socket"]
    sk.send({"action": "move", "x": 4, "y": 4})
    assert H.wait(qapp, lambda: rec.first("aiMove") is not None, timeout=30.0)
    rec.clear()
    sk.send({"action": "takeback", "plies": 2})
    assert H.wait(qapp, lambda: rec.first("takeback") is not None, timeout=20.0), rec.types
    ev = rec.first("takeback")
    assert len(ev["undone"]) == 2 and ev["moveCount"] == 0
    assert ev["state"]["moveCount"] == 0
    assert ev["state"]["board"][4][4] == 0, "回滚后棋盘上不该还留着那一子"
    # 曲线也得跟着退：留着悔过的那两手，图就是假的
    assert len(ev["state"]["curve"]) <= 1


def test_untimed_game_sends_no_countdown(host, qapp):
    """`moveSeconds=0` 是“不限时”而不是“0 秒”。两者在页面上差一个会自己判负的计时器。"""
    user = U.register(host, tag="no")
    gid = U.create_game(host, user["token"], moveSeconds=0)["game"]["id"]
    sk, rec = U.connect(host, user["token"], gid)
    try:
        assert H.wait(qapp, lambda: rec.first("state") is not None, timeout=30.0)
        assert rec.state["moveSeconds"] == 0 and rec.state["moveSecondsLeft"] is None
        rec.clear()
        sk.send({"action": "move", "x": 3, "y": 3})
        assert H.wait(qapp, lambda: rec.first("aiMove") is not None, timeout=30.0)
        # 必须是 None，不能是 0 或某个残值 —— 页面按它决定倒计时那一行画不画，
        # 给 0 会凭空冒出一个“本手已超时”然则对局根本不会结束
        assert rec.first("aiMove")["moveSecondsLeft"] is None
    finally:
        sk.close()


def test_timed_game_hands_out_remaining_seconds(host, qapp):
    user = U.register(host, tag="cl")
    res = U.create_game(host, user["token"], moveSeconds=25)
    gid = res["game"]["id"]
    sk, rec = U.connect(host, user["token"], gid)
    try:
        assert H.wait(qapp, lambda: rec.first("state") is not None, timeout=30.0)
        assert rec.state["moveSeconds"] == 25
        assert 0 < rec.state["moveSecondsLeft"] <= 25, "第一手就要在计时"
        rec.clear()
        sk.send({"action": "move", "x": 4, "y": 4})
        assert H.wait(qapp, lambda: rec.first("aiMove") is not None, timeout=30.0)
        left = rec.first("aiMove")["moveSecondsLeft"]
        assert 0 < left <= 25, f"AI 落子后应重新发一手的时间，实到 {left}"
    finally:
        sk.close()


def test_hint_arrives_as_hint_only(game, qapp):
    rec, sk = game["rec"], game["socket"]
    rec.clear()
    sk.send({"action": "hint"})
    assert H.wait(qapp, lambda: rec.first("hintOnly") is not None, timeout=30.0), rec.types
    ev = rec.first("hintOnly")
    assert isinstance(ev["hint"], list) and ev["hint"], "启发式引擎也该给出候选点"
    assert "winrateBlack" in ev and "ownership" in ev
    assert len(ev["ownership"]) == 81, "ownership 是 size² 的一维数组，差一个棋盘就画歪一片"


def test_reconnect_brings_a_fresh_full_state(game, qapp):
    rec, sk = game["rec"], game["socket"]
    sk.send({"action": "move", "x": 4, "y": 4})
    assert H.wait(qapp, lambda: rec.first("aiMove") is not None, timeout=30.0)
    rec.clear()
    sk._ws.close()          # 模拟掉线：不是用户主动关的，所以该自动重连
    assert H.wait(qapp, lambda: "reconnecting" in rec.statuses, timeout=10.0), rec.statuses
    assert H.wait(qapp, lambda: rec.count("state") >= 1, timeout=20.0), \
        f"重连后服务端要推全量 state：statuses={rec.statuses} err={sk.last_error}"
    assert rec.state["moveCount"] >= 2, "补回来的 state 必须是当前局面，不是开局"
    assert H.wait(qapp, lambda: sk.is_open, timeout=20.0)


def test_bogus_token_is_rejected_then_stays_closeable(host, qapp):
    user = U.register(host, tag="tk")
    gid = U.create_game(host, user["token"])["game"]["id"]
    sk, rec = U.connect(host, "zhe-bu-shi-yi-ge-you-xiao-token", gid)
    try:
        assert H.wait(qapp, lambda: rec.first("error") is not None, timeout=15.0), rec.types
        assert "登录" in rec.first("error")["message"]
        sk.close()
        assert sk.status == "closed"
    finally:
        sk.close()


# ------------------------------------------------------------------ 终局

def test_resign_ends_the_game_and_reports_rank(game, qapp):
    rec, sk = game["rec"], game["socket"]
    sk.send({"action": "move", "x": 4, "y": 4})
    assert H.wait(qapp, lambda: rec.first("aiMove") is not None, timeout=30.0)
    rec.clear()
    sk.send({"action": "resign"})
    assert H.wait(qapp, lambda: rec.first("gameEnd") is not None, timeout=20.0), rec.types
    end = rec.first("gameEnd")
    assert end["reason"] == "player-resign" and end["winner"] == 2
    assert end["playerWon"] is False
    # `countsForRank` 是强制结束专用的字段（`types.ts` 里就是可选），普通终局不带它
    # → 页面必须把“没这个键”当作计入战绩，写成 `is True` 会把每一局都误判成作废
    assert end.get("countsForRank", True) is not False
    assert end["rank"]["progress"]["rankName"], "认输也要带回段位，导航栏徽章要跟着变"
    assert end["sgf"].startswith("(;"), f"终局该带整份棋谱：{end['sgf'][:40]!r}"
    # 已结束的局再发指令：还是 error，但连接不该被踢
    rec.clear()
    sk.send({"action": "pass"})
    assert H.wait(qapp, lambda: rec.first("error") is not None, timeout=15.0)
    assert sk.is_open


def test_force_end_via_rest_still_reaches_the_socket(game, qapp):
    """强制结束只有 REST 入口（它得能对内存里没有的卡死对局用），
    但客户端是连着 WS 的 —— 后端会 emit 同一条事件，界面不能卡在"还在下"。"""
    import threading

    rec = game["rec"]

    def post():
        A.http_json("POST", f"{game['host'].base_url}/api/games/{game['id']}/force-end",
                    None, game["user"]["token"])
    threading.Thread(target=post, daemon=True).start()
    assert H.wait(qapp, lambda: rec.first("gameEnd") is not None, timeout=25.0), rec.types
    end = rec.first("gameEnd")
    assert end["reason"] == "force-end" and end["winner"] == 0
    assert end["countsForRank"] is False and end["rank"] is None, \
        "作废局不能带段位变动，否则徽章会假跳一级"


def test_endgame_scoring_dead_correction_and_final_result(host, qapp):
    """填单官 → scoring → 修正死子 → scoreConfirm → gameEnd。客户端视角的完整终局。

    服务端结算的数学（提子后怎么数地）由后端 199 项保证；这里钉的是
    **事件形状与界面要用的字段**，以及"玩家点掉的死子确实进了最终判定"。
    """
    user = U.register(host, tag="en")
    res = U.create_game(host, user["token"])
    gid = res["game"]["id"]
    sk, rec = U.connect(host, user["token"], gid)
    try:
        assert H.wait(qapp, lambda: rec.first("state") is not None, timeout=30.0)
        ev, grid = U.play_out_to_scoring(host, user["token"], gid, sk, rec, qapp, H.wait)
        assert ev["type"] == "scoring"
        assert "deadStones" in ev and "preview" in ev and "message" in ev
        prev = ev["preview"]
        for key in ("blackStones", "whiteStones", "blackTerritory", "whiteTerritory",
                    "blackTotal", "whiteTotal", "komi", "result", "method", "winner"):
            assert key in prev, f"结算面板要用 preview.{key}，服务端没给"
        assert prev["method"] == "area"
        stones_before = prev["blackStones"] + prev["whiteStones"]
        # 客户端点改死子：**从盘上真有的子里挑**，不拿 `ev["deadStones"]` 当唯一依据。
        # 原先那句 `assert engine_dead`（「这个形状下引擎该判出死子」）在 KataGo 那一支
        # 一开就红：`set_up_endgame` 只改服务端 grid、不改 moves，而 `build_query` 按
        # `gtp_moves()` 重放 —— KataGo 看到的是「9 枚散子」，判出 0 死子是**正确答案**；
        # 启发式那边能判出死子反而是它的误判（L10）。把前提建在一家引擎的输出上，
        # 两支里必然有一支不对。这里要钉的是「客户端给的表服务端照收、并按它重算」，
        # 与引擎判了什么无关。依旧不写死坐标：AI 临场填了哪些单官决定了盘上
        # 每个点是什么颜色，写死的 [7,1] 在别的 run 里可能本来就空着（那种情况下断言会假绿）。
        size = U.ENDGAME_SIZE
        engine_dead = {(int(x), int(y)) for x, y in ev["deadStones"]}
        on_board = [(x, y) for y in range(size) for x in range(size) if grid[y][x]]
        assert len(on_board) >= 5, f"盘上可点的子太少，测不到点改：{len(on_board)}"
        # 引擎判死的那几枚里挑 2 枚**不提交** = 用户在面板上点了「救活」；
        # KataGo 那一支这个集合是空的，此时只是少测了「撤销引擎判定」这一支，
        # 主路径（提交什么就按什么算）仍然精确成立。
        revived = sorted(engine_dead)[:2]
        submitted = [p for p in on_board if p not in revived][:3]
        assert len(submitted) == 3, f"提交不出 3 枚死子：{submitted} vs {revived}"
        rec.clear()
        sk.send({"action": "scoreConfirm", "dead": [list(p) for p in submitted]})
        assert H.wait(qapp, lambda: rec.first("gameEnd") is not None, timeout=20.0), rec.types
        end = rec.first("gameEnd")
        assert end["reason"] == "pass-pass"
        assert end.get("countsForRank", True) is not False
        res = end["result"]
        # preview 按引擎那份死子表数（engine_dead 个），result 按客户端给的表数
        # （submitted 个）：两边净剩子数之差必须精确等于两个个数之差。
        inc = ((res["blackStones"] - prev["blackStones"]) +
               (res["whiteStones"] - prev["whiteStones"]))
        assert inc == len(engine_dead) - len(submitted), (
            f"最终判定没按客户端给的死子表重算：{stones_before} -> "
            f"{res['blackStones'] + res['whiteStones']}，引擎判死 {len(engine_dead)} 枚、"
            f"客户端提交 {len(submitted)} 枚")
        # 比集合而不是比列表：`scoring.deadStones` 是盘面扫描顺序（行优先），
        # `result.deadStones` 是 `sorted(集合)`（坐标序）—— 同一批点两种排法，
        # 拿它当顺序断言会隔一个 run 假失败一次。
        assert {tuple(p) for p in res["deadStones"]} == set(submitted), \
            "服务端要以客户端给的死子表为准，不能自己重算一遍"
        assert len(res["deadStones"]) == len(submitted), "死子个数不能被服务端悄悄增删"
        assert end["rank"]["progress"]["rankName"]
        assert end["resultText"], "结算文案要能直接显示"
    finally:
        sk.close()
