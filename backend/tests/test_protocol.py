"""KataGo 协议层测试：查询线路格式 + 视角换算 + 响应流解析。

这里的断言全部来自对真实 KataGo v1.17.1 的实测，不是照文档写的：
  * moves 必须是配对数组，对象数组会被直接拒；
  * reportAnalysisWinratesAs 是配置级键，放进查询会触发同 id 的字段告警；
  * humanSLProfile 必须放进 overrideSettings，放顶层会 FATAL ERROR 杀进程；
  * scoreLead / ownership 都是**走子方视角**，ownership 行序还是自上而下。
这些约定错一个就是静默的数值颠倒，所以逐条钉住。
"""
from __future__ import annotations

import asyncio
import json

from app.engine.katago import KataGoEngine
from app.engine.protocol import (AnalysisQuery, _convert_ownership, parse_response,
                                 parse_multi_response)
from app.game.rules import BLACK, WHITE


def kata_response(score_lead: float = 0.0, winrate: float = 0.5, moves=None,
                  cand_score: float = 0.0) -> dict:
    """造一份 KataGo analysis 的单条响应（字段名与真实输出一致）。"""
    return {
        "id": "q1",
        "moveInfos": [
            {"move": mv, "visits": 10 - i, "winrate": winrate, "scoreLead": cand_score,
             "pv": [mv]}
            for i, mv in enumerate(moves or ["E5", "C5"])
        ],
        "rootInfo": {"scoreLead": score_lead, "winrate": winrate, "visits": 20},
    }


# ---------------------------------------------------------------------------
# 查询线路格式
# ---------------------------------------------------------------------------
def test_moves_serialise_as_pairs():
    """KataGo 要 [["B","E5"]]，发成 [{"player":"B","move":"E5"}] 会被直接拒。"""
    q = AnalysisQuery(size=9, moves=[{"player": "B", "move": "E5"},
                                     {"player": "W", "move": "C3"}])
    assert q.to_json()["moves"] == [["B", "E5"], ["W", "C3"]]


def test_moves_accept_pair_form_too():
    """已经是配对的写法（部分调用方这么构造）不能被二次包装。"""
    q = AnalysisQuery(size=9, moves=[["B", "E5"], ["W", "C3"]])
    assert q.to_json()["moves"] == [["B", "E5"], ["W", "C3"]]


def test_empty_moves_are_indistinguishable_so_guard_the_nonempty_case():
    """空数组两种写法都合法 —— 这正是格式错误能溜过冒烟测试的原因。"""
    assert AnalysisQuery(size=9, moves=[]).to_json()["moves"] == []


def test_no_config_level_keys_in_query():
    """reportAnalysisWinratesAs 属于 analysis.cfg；出现在查询里会引来同 id 的字段告警，
    而告警带着同一个 id，很容易被 _read_loop 当成结果交出去。"""
    q = AnalysisQuery(size=9, moves=[], max_visits=8)
    assert "reportAnalysisWinratesAs" not in q.to_json()


def test_human_sl_profile_goes_into_override_settings():
    """顶层 humanSLProfile 会让 KataGo 直接 FATAL ERROR 退出（实测），必须走 overrides。"""
    q = AnalysisQuery(size=9, moves=[], human_sl_profile="preaz_18k")
    payload = q.to_json()
    assert "humanSLProfile" not in payload
    assert payload["overrideSettings"] == {"humanSLProfile": "preaz_18k"}


def test_analyze_turns_only_when_set_and_no_max_moves():
    """analyzeTurns 只在需要时出现；maxMoves 必须**永远不出现**。

    v1.17.1 既不认 maxMoves 这个查询字段也不认这个配置键（两边都实测过），
    发过去会换来一条同 id 的字段告警，把后续响应错位；返回多少候选完全由
    maxVisits 决定，所以复盘要提高命中率只能调 review_visits，而不是 maxMoves。
    """
    q = AnalysisQuery(size=9, moves=[], analyze_turns=[0, 1])
    payload = q.to_json()
    assert payload["analyzeTurns"] == [0, 1]
    assert "maxMoves" not in payload
    bare = AnalysisQuery(size=9, moves=[]).to_json()
    assert "analyzeTurns" not in bare and "maxMoves" not in bare


# ---------------------------------------------------------------------------
# 视角换算
# ---------------------------------------------------------------------------
def test_score_lead_is_converted_to_black_view():
    """白先走时 KataGo 的 scoreLead 是「白领先为正」，必须取负换成黑视角。"""
    white_to_move = parse_response(kata_response(score_lead=-24.8), 9, side_to_move=WHITE)
    black_to_move = parse_response(kata_response(score_lead=25.0), 9, side_to_move=BLACK)
    assert white_to_move.score_lead > 24.0, "黑必胜局面，黑视角必须为正"
    assert black_to_move.score_lead > 24.0
    # 同一个必胜局面，两种走子方换算后应当同号同量级
    assert abs(white_to_move.score_lead - black_to_move.score_lead) < 1.5


def test_candidate_score_lead_uses_the_same_view():
    """候选点的 scoreLead 与 rootInfo 必须用同一个视角换算，否则 node_loss 的差值没意义。"""
    raw = kata_response(score_lead=-10.0, cand_score=-8.0)
    as_white = parse_response(raw, 9, side_to_move=WHITE)
    as_black = parse_response(raw, 9, side_to_move=BLACK)
    assert as_white.score_lead > 0 and all(c.score_lead > 0 for c in as_white.candidates)
    # 同一份原始数据只差走子方，结果应当恰好反号：root 与候选一致地翻了
    assert as_white.score_lead == -as_black.score_lead
    assert as_white.candidates[0].score_lead == -as_black.candidates[0].score_lead


def test_winrate_stays_side_to_move():
    """winrate 不换算：KataGo 已是走子方视角，winrate_black 由它推导。"""
    res = parse_response(kata_response(winrate=0.009), 9, side_to_move=WHITE)
    assert res.winrate < 0.02, "白先走且白必败"
    assert res.winrate_black > 0.98
    assert res.winrate_white < 0.02


def test_ownership_sign_and_row_order_converted():
    """ownership 要同时翻符号（走子方→白方为正）和翻行序（顶部→底部）。

    构造：3 路盘，KataGo 原始数组行0 = 顶部 = +1、行2 = 底部 = -1。
      黑先走（正=黑）：顶部黑地、底部白地 → 内部白方为正且 y=0 在底部，
                     所以底部 +1、顶部 -1（行序与符号都翻了）；
      白先走（正=白）：顶部白地、底部黑地 → 底部 -1、顶部 +1（只翻行序）。
    """
    raw = [+1.0, +1.0, +1.0,
           0.0, 0.0, 0.0,
           -1.0, -1.0, -1.0]
    # 黑先走：顶部黑地/底部白地 → 内部（白为正、y=0 在底）底部 +1、顶部 -1
    assert _convert_ownership(raw, 3, BLACK) == [+1.0, +1.0, +1.0,
                                                 0.0, 0.0, 0.0,
                                                 -1.0, -1.0, -1.0]
    # 白先走：顶部白地/底部黑地 → 内部底部 -1、顶部 +1
    assert _convert_ownership(raw, 3, WHITE) == [-1.0, -1.0, -1.0,
                                                 0.0, 0.0, 0.0,
                                                 +1.0, +1.0, +1.0]


def test_ownership_black_view_points_at_black_territory():
    """前端热力图直接吃 ownership_black_view：黑地必须是正。"""
    raw = [+1.0, +1.0, +1.0, 0.0, 0.0, 0.0, -1.0, -1.0, -1.0]   # 黑先走，顶部黑地
    res = parse_response({**kata_response(), "ownership": raw}, 3, side_to_move=BLACK)
    view = res.ownership_black_view(3)
    # 内部 y=2 是顶部（GTP 第3行），黑地 → 正
    assert all(v > 0.5 for v in view[2 * 3:3 * 3])
    assert all(v < -0.5 for v in view[0:3])


def test_ownership_bad_length_is_dropped():
    """长度对不上就整片丢弃：错位的 ownership 会让死子判定认错行，宁可没有。"""
    assert _convert_ownership([1.0, 2.0], 3, BLACK) == []
    assert _convert_ownership([], 3, BLACK) == []


# ---------------------------------------------------------------------------
# 响应流解析
# ---------------------------------------------------------------------------
class _FakeStream:
    """伪造引擎输出流（按 _read_loop 的分块 read 语义实现）。"""

    def __init__(self, lines: list[bytes]):
        self._data = b"".join(lines)
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        if self._pos >= len(self._data):
            await asyncio.sleep(5)      # 模拟流未关闭但没数据
            return b""
        n = len(self._data) - self._pos if n < 0 else min(n, len(self._data) - self._pos)
        chunk = self._data[self._pos:self._pos + n]
        self._pos += n
        return chunk


class _StubProc:
    def __init__(self, lines: list[bytes]):
        self.stdout = _FakeStream(lines)
        self.stdin = None
        self.returncode = None


def _feed(lines: list[bytes], request_id: str, expect: int = 1):
    """把伪造的输出喂给 _read_loop，取回它交给 Future 的东西。"""

    async def run():
        eng = KataGoEngine()
        eng.proc = _StubProc(lines)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        eng._pending[request_id] = fut
        if expect > 1:
            eng._expect[request_id] = expect
        task = asyncio.create_task(eng._read_loop())
        try:
            return await asyncio.wait_for(fut, timeout=3)
        finally:
            task.cancel()

    return asyncio.run(run())


def test_read_loop_accumulates_analyze_turns_lines():
    """analyzeTurns 是**每手一行、id 相同**：第一行就交出会让复盘只剩第 0 手。"""
    lines = [(json.dumps({"id": "t1", "turnNumber": i,
                          "rootInfo": {"scoreLead": float(i), "visits": 5}})
              + "\n").encode() for i in range(3)]
    data = _feed(lines, "t1", expect=3)
    assert isinstance(data, list), "必须整包交出，不能只给第一行"
    assert [d["turnNumber"] for d in data] == [0, 1, 2]
    parsed = parse_multi_response(data, 9, [BLACK, WHITE, BLACK])
    assert len(parsed) == 3
    assert [p.turn for p in parsed] == [0, 1, 2]


def test_read_loop_sorts_turns_before_handing_over():
    """side_to_move_seq 按下标对齐，乱序到达必须重排，否则行棋方会错配。"""
    lines = [(json.dumps({"id": "t2", "turnNumber": n,
                          "rootInfo": {"scoreLead": 0.0, "visits": 5}})
              + "\n").encode() for n in (2, 0, 1)]
    data = _feed(lines, "t2", expect=3)
    assert [d["turnNumber"] for d in data] == [0, 1, 2]


def test_read_loop_ignores_field_warning():
    """字段告警带着同一个 id，当成结果交出会让调用方拿到一份空分析。"""
    lines = [
        b'{"id":"w1","warning":"Unexpected or unused field"}\n',
        b'{"id":"w1","moveInfos":[],"rootInfo":{"scoreLead":1.5,"winrate":0.5,"visits":8}}\n',
    ]
    data = _feed(lines, "w1")
    assert "warning" not in data
    assert data["rootInfo"]["scoreLead"] == 1.5


def test_read_loop_resolves_error_response_immediately():
    """错误响应只有一行，攒不齐 expect 行，必须立刻交出否则只能等超时。"""
    lines = [b'{"id":"e1","error":"Must be an array of pairs","field":"moves"}\n']
    data = _feed(lines, "e1", expect=4)
    assert data.get("error", "").startswith("Must be an array")


def test_read_loop_handles_lines_over_64kb():
    """19 路高 visit 的 analyzeTurns 单行可超 64KB（asyncio readline 的硬上限）。

    超限会让读循环抛 LimitOverrunError 且数据被丢弃，之后所有在飞查询只能
    干等超时——这就是线上「批量分析失败（空错误信息）」的根因。
    """
    big = "x" * (200 * 1024)
    lines = [
        (json.dumps({"id": "big1", "turnNumber": 0, "blob": big,
                     "rootInfo": {"scoreLead": 1.0, "visits": 5}})
         + "\n").encode(),
        b'{"id":"big1","turnNumber":1,"rootInfo":{"scoreLead":2.0,"visits":5}}\n',
    ]
    data = _feed(lines, "big1", expect=2)
    assert [d["turnNumber"] for d in data] == [0, 1]
