"""复盘分析测试：损失目数口径、问题手分级、吻合度、阶段统计、报告生成。

重点锁住「同节点损失」这条口径：它是唯一对任何估值函数都成立的算法，
跳节点差值只在引擎值函数可信（KataGo）时才作兜底。

后半是**无大模型时的模板讲解**（commentary）：那才是默认路径——绝大多数用户
没配 API Key，看到的每一句都是模板写的，它一旦把噪声当事实说就没有东西兜得住。
"""
from __future__ import annotations

import asyncio

from app.game.rules import BLACK, WHITE, to_gtp
from app.review import analyzer, commentary
from app.review.analyzer import (FLAG_BAD, FLAG_BLUNDER, FLAG_GOOD, FLAG_PASS,
                                 FLAG_SLOW)

SIZE = 9


def cand(gtp: str, x: int, y: int, score_lead: float, winrate: float = 0.5,
         rank: int = 0) -> dict:
    return {"gtp": gtp, "x": x, "y": y, "visits": 10 - rank, "winrate": winrate,
            "scoreLead": score_lead, "policy": None, "pv": [], "pvPoints": [],
            "rank": rank}


def node(score_lead: float, candidates: list[dict], side: int = 1,
         engine: str = "heuristic") -> dict:
    """一个 analyses[i] 条目（scoreLead 固定黑视角，winrateBlack/White 由此派生）。"""
    wr_black = 0.5 + score_lead / 100.0
    return {
        "turn": 0, "sideToMove": side, "scoreLead": score_lead,
        "scoreLeadWhite": -score_lead, "winrateBlack": round(wr_black, 4),
        "winrateWhite": round(1 - wr_black, 4), "visits": 32, "engine": engine,
        "candidates": candidates,
    }


# ---------------------------------------------------------------------------
# 同节点损失
# ---------------------------------------------------------------------------
def test_node_loss_measures_gap_to_best_candidate():
    """黑先：首选 +3.0，实际落点 -1.5 → 损失 4.5 目。"""
    before = node(-2.0, [
        cand("E5", 4, 4, 3.0, rank=0),
        cand("C3", 2, 2, 1.0, rank=1),
        cand("A1", 0, 0, -1.5, rank=2),
    ])
    mv = {"x": 0, "y": 0, "color": 1}
    assert analyzer.node_loss(before, mv, 1) == 4.5


def test_node_loss_is_zero_when_player_follows_engine():
    before = node(0.0, [cand("E5", 4, 4, 2.0, rank=0), cand("C3", 2, 2, 0.5, rank=1)])
    assert analyzer.node_loss(before, {"x": 4, "y": 4, "color": 1}, 1) == 0.0


def test_node_loss_uses_mover_perspective_for_white():
    """白先：scoreLead 是黑视角，白方的「好」是数值更小，取符号后比大小。"""
    before = node(1.0, [
        cand("E5", 4, 4, -2.0, rank=0),      # 白方最佳：黑目差最小
        cand("C3", 2, 2, 0.0, rank=1),
        cand("A1", 0, 0, 3.0, rank=2),       # 白方最差
    ], side=2)
    loss = analyzer.node_loss(before, {"x": 0, "y": 0, "color": 2}, 2)
    assert loss == 5.0


def test_node_loss_returns_none_when_move_not_listed():
    """落点不在候选里 → 交给调用方兜底，不能凭空诬陷为 0 或乱猜。"""
    before = node(0.0, [cand("E5", 4, 4, 2.0, rank=0)])
    assert analyzer.node_loss(before, {"x": 8, "y": 8, "color": 1}, 1) is None


def test_node_loss_handles_missing_and_pass():
    assert analyzer.node_loss(None, {"x": 0, "y": 0}, 1) is None
    assert analyzer.node_loss({"missing": True}, {"x": 0, "y": 0}, 1) is None
    assert analyzer.node_loss(node(0.0, [cand("E5", 4, 4, 1.0)]),
                              {"x": None, "y": None}, 1) is None


def test_node_loss_winrate_matches_mover_view():
    """候选 winrate 本就是走子方视角，无需按颜色翻转。"""
    before = node(0.0, [
        cand("E5", 4, 4, 1.0, winrate=0.62, rank=0),
        cand("A1", 0, 0, -1.0, winrate=0.41, rank=1),
    ])
    assert round(analyzer.node_loss_winrate(before, {"x": 0, "y": 0}), 4) == 0.21


# ---------------------------------------------------------------------------
# 报告组装
# ---------------------------------------------------------------------------
def _sample_game():
    """3 手棋：黑好手 → 白大恶手 → 黑缓手，用于校验分级与统计。

    坐标与 GTP 名一一对应（9 路盘跳过 I 列）：
      (2,2)=C3  (4,4)=E5  (3,3)=D4  (6,6)=G7  (2,6)=C7  (5,5)=F6
    """
    moves = [
        {"color": 1, "x": 2, "y": 2, "gtp": "C3", "captures": []},
        {"color": 2, "x": 6, "y": 6, "gtp": "G7", "captures": []},
        {"color": 1, "x": 2, "y": 6, "gtp": "C7", "captures": []},
    ]
    analyses = [
        # 黑先：首选 E5(+3.0)，实下 C3(+2.9) → 损失 0.1 目
        node(-2.0, [cand("E5", 4, 4, 3.0, rank=0), cand("C3", 2, 2, 2.9, rank=1)]),
        # 白先：首选 D4（黑视角 -4.0），实下 G7（黑视角 +7.0）→ 损失 11 目
        node(1.0, [cand("D4", 3, 3, -4.0, rank=0), cand("G7", 6, 6, 7.0, rank=1)], side=2),
        # 黑先：首选 F6(+4.0)，实下 C7(+1.5) → 损失 2.5 目
        node(-1.0, [cand("F6", 5, 5, 4.0, rank=0), cand("C7", 2, 6, 1.5, rank=1)]),
        node(-1.0, [cand("G7", 6, 6, 2.0, rank=0)]),
    ]
    return moves, analyses


def test_build_move_reports_prefers_within_node_loss():
    moves, analyses = _sample_game()
    reports = analyzer.build_move_reports(moves, analyses, SIZE, player_color=1)

    assert reports[0]["lossPoints"] == 0.1      # 同节点：3.0 - 2.9
    assert reports[1]["lossPoints"] == 11.0     # 白方取符号后：4.0 - (-7.0)
    assert reports[2]["lossPoints"] == 2.5
    assert reports[0]["flag"] == FLAG_GOOD
    assert reports[1]["flag"] == FLAG_BLUNDER
    assert reports[2]["flag"] == FLAG_SLOW
    assert reports[1]["playerRank"] == 1
    assert reports[1]["bestMove"]["gtp"] == "D4"
    # 跳节点差值仍作为展示数据保留（前端目差曲线与「这一手前后」对比用）
    assert reports[0]["scoreBefore"] == -2.0
    assert reports[0]["scoreAfter"] == 1.0


def test_flag_thresholds_follow_settings():
    from app.config import settings
    assert analyzer.classify(settings.mistake_blunder) == FLAG_BLUNDER
    assert analyzer.classify(settings.mistake_bad) == FLAG_BAD
    assert analyzer.classify(settings.mistake_slow) == FLAG_SLOW
    assert analyzer.classify(settings.mistake_slow - 0.01) == FLAG_GOOD


def test_falls_back_to_cross_node_when_move_unlisted():
    """落点不在候选里时退回跳节点差值（KataGo 的值函数可信）。"""
    moves = [{"color": 1, "x": 8, "y": 8, "gtp": "J9", "captures": []}]
    analyses = [node(2.0, [cand("E5", 4, 4, 3.0, rank=0)], engine="katago"),
                node(-2.0, [cand("D4", 3, 3, 0.0, rank=0)], side=2, engine="katago")]
    reports = analyzer.build_move_reports(moves, analyses, SIZE, player_color=1)
    assert reports[0]["lossPoints"] == 4.0      # 2.0 - (-2.0)
    assert reports[0]["scoreBefore"] == 2.0
    assert reports[0]["scoreAfter"] == -2.0


def test_pass_move_is_not_accused():
    moves = [{"color": 1, "x": None, "y": None, "gtp": "pass", "captures": []}]
    analyses = [node(0.0, [cand("E5", 4, 4, 1.0, rank=0)]),
                node(-5.0, [cand("E5", 4, 4, 1.0, rank=0)], side=2)]
    reports = analyzer.build_move_reports(moves, analyses, SIZE, player_color=1)
    assert reports[0]["flag"] == FLAG_PASS


def test_missing_analysis_defaults_to_good():
    """引擎少返回数据时不能把玩家判成问题手。"""
    moves = [{"color": 1, "x": 4, "y": 4, "gtp": "E5", "captures": []}]
    reports = analyzer.build_move_reports(moves, [{"missing": True}, {"missing": True}],
                                          SIZE, player_color=1)
    assert reports[0]["flag"] == FLAG_GOOD
    assert reports[0]["lossPoints"] is None


# ---------------------------------------------------------------------------
# 汇总指标
# ---------------------------------------------------------------------------
def test_accuracy_and_flag_counts_split_by_color():
    moves, analyses = _sample_game()
    reports = analyzer.build_move_reports(moves, analyses, SIZE, player_color=1)

    assert analyzer.accuracy_of(reports, 1) == round((0.1 + 2.5) / 2, 3)
    assert analyzer.accuracy_of(reports, 2) == 11.0
    counts = analyzer.flag_counts(reports, 1)
    assert counts[FLAG_GOOD] == 1 and counts[FLAG_SLOW] == 1
    assert sum(counts.values()) == 2          # 只统计黑方两手


def test_accuracy_of_returns_none_without_data():
    assert analyzer.accuracy_of([], 1) is None
    assert analyzer.accuracy_of([{"color": 1, "flag": FLAG_PASS, "lossPoints": None}], 1) is None


def test_phase_of_splits_opening_middle_endgame():
    assert analyzer.phase_of(5, 200, 19) == "opening"
    assert analyzer.phase_of(100, 200, 19) == "middle"
    assert analyzer.phase_of(195, 200, 19) == "endgame"


def test_phase_stats_reports_worst_move():
    moves, analyses = _sample_game()
    reports = analyzer.build_move_reports(moves, analyses, SIZE, player_color=1)
    phases = analyzer.phase_stats(reports, 1, len(moves), SIZE)
    opening = phases["opening"]
    assert opening["label"] == "布局"
    assert opening["moves"] == 2
    assert opening["worstMoveNum"] == 3         # 第 3 手损失 2.5 > 第 1 手 0.1
    assert opening["totalLoss"] == 2.6


def test_key_moves_sorted_by_move_number():
    moves, analyses = _sample_game()
    reports = analyzer.build_move_reports(moves, analyses, SIZE, player_color=1)
    reports[0]["flag"] = FLAG_SLOW              # 把第 1 手也标成问题手，凑出两处
    key = analyzer.key_moves(reports, 1)
    assert [r["moveNum"] for r in key] == [1, 3]
    assert [r["lossPoints"] for r in key] == [0.1, 2.5]     # 先按损失排序，再按手数回排


def test_build_curve_skips_missing_and_top_moments_picks_largest_swing():
    moves, analyses = _sample_game()
    curve = analyzer.build_curve(analyses)
    assert [c["ply"] for c in curve] == [0, 1, 2, 3]

    moments = analyzer.top_moments(analyses, moves, player_color=1, limit=1)
    assert len(moments) == 1
    assert moments[0]["moveNum"] == 1           # 黑视角胜率 0.48 → 0.51，波动最大


# ---------------------------------------------------------------------------
# 无大模型时的模板讲解（commentary）
# ---------------------------------------------------------------------------
def report_of(played, best, loss, wr_loss=0.0, captures=0, best_pv=None,
              move_num=5, before_sl=0.0, after_sl=0.0, size=SIZE,
              player_color=BLACK):
    """走一遍真实 analyzer 产出一份报告，不手写字段名。

    首选与实际落点都放进候选（同节点口径），所以 lossPoints 恰好等于 loss：
    首选 sl = ±loss、落点 sl = 0，按符号取差就是 loss。moveNum 直接覆盖，
    它只是个阶段标签，不影响其他字段的自洽。
    """
    sign = 1.0 if player_color == BLACK else -1.0
    best_c = cand(to_gtp(best, size), best[0], best[1], sign * loss,
                  winrate=0.60, rank=0)
    best_c["pvPoints"] = best_pv or []
    played_c = cand(to_gtp(played, size), played[0], played[1], 0.0,
                    winrate=round(0.60 - wr_loss, 4), rank=1)
    other = WHITE if player_color == BLACK else BLACK
    moves = [{"color": player_color, "x": played[0], "y": played[1],
              "gtp": to_gtp(played, size), "captures": [0] * captures}]
    analyses = [node(before_sl, [best_c, played_c], side=player_color),
                node(after_sl, [cand("A1", 0, 0, 0.0)], side=other)]
    rep = analyzer.build_move_reports(moves, analyses, size, player_color)[0]
    rep["moveNum"] = move_num
    return rep


def test_template_comment_direction_kind_names_distance_and_points():
    """远离引擎首选 → 归为「战场方向」，并把首选点、距离、方位写进文本。"""
    r = report_of((0, 0), (4, 4), 6.0)          # 9 路：dist 4 ≥ far(4)
    assert r["lossPoints"] == 6.0               # helper 自身的口径先验一下
    assert commentary.diagnose({**r, "totalMoves": 60}, SIZE, False) == commentary.K_DIR
    c = commentary.template_comment(r, 60, SIZE)
    assert "E5" in c["advice"] and "4 路" in c["advice"]
    assert "往右上方" in c["advice"]
    assert "6.0 目" in c["reason"]
    assert c["maxim"]


def test_template_comment_local_kind_when_close_to_preferred():
    """就在首选旁边时不能批评「方向错」——那会把学生往错的地方支。"""
    r = report_of((4, 4), (4, 5), 6.0)          # dist 1 ≤ near(2)
    assert commentary.diagnose({**r, "totalMoves": 60}, SIZE, False) == commentary.K_LOCAL
    c = commentary.template_comment(r, 60, SIZE)
    assert "方向与引擎一致" in c["reason"]
    assert "同一带" in c["advice"]
    assert "往右上方" not in c["advice"]         # 1 路不算方位，只说「就在附近」


def test_bearing_uses_board_orientation_not_raw_axis_signs():
    """方位词必须与棋盘上看到的朝向一致（内部 y 大 = 靠上）。

    说反方向比不说更糟，所以这条按渲染代码的换算（GoBoard.toPixel 用
    `py = pad + (size-1-y)*cell`）把四个斜向与两个正向钉死。
    """
    assert commentary._bearing((4, 4), (6, 6), 9) == "往右上方"
    assert commentary._bearing((4, 4), (2, 2), 9) == "往左下方"
    assert commentary._bearing((4, 4), (4, 7), 9) == "往上方"
    assert commentary._bearing((4, 4), (7, 4), 9) == "往右方"
    assert commentary._bearing((4, 4), (4, 5), 9) == "就在附近"
    assert commentary._bearing((4, 4), None, 9) == ""


def test_order_kind_when_played_point_appears_in_preferred_pv():
    """落点是引擎首选变化里的一步 → 选点没错，是次序问题（不能骂方向）。"""
    r = report_of((0, 0), (4, 4), 6.0, best_pv=[[4, 4], [5, 5], [0, 0]])
    assert commentary.diagnose({**r, "totalMoves": 60}, SIZE, False) == commentary.K_ORDER
    c = commentary.template_comment(r, 60, SIZE)
    assert "次序" in c["reason"]
    assert "对方下一手" not in c["advice"]      # 不该再用方向类的结语
    # pv 的第 0 个就是首选本身，不能拿它当成「在变化里」（那等于说首选=落点却没损失）
    r0 = report_of((4, 4), (4, 4), 6.0, best_pv=[[4, 4]])
    assert commentary.diagnose({**r0, "totalMoves": 60}, SIZE, False) != commentary.K_ORDER


def test_risk_kind_needs_trustworthy_winrate():
    """目损不大但胜率掉得多 → 「棋的安危」；这个判据在无 KataGo 时必须失效。

    它靠的是两个估值数的背离，启发式引擎两个都不准，凑不出这个信号。
    """
    r = report_of((0, 0), (4, 4), 2.5, wr_loss=0.30)
    assert commentary.diagnose({**r, "totalMoves": 60}, SIZE, False) == commentary.K_RISK
    assert commentary.diagnose({**r, "totalMoves": 60}, SIZE, True) == commentary.K_DIR
    assert "安危" in commentary.template_comment(r, 60, SIZE, False)["reason"]
    lo = commentary.template_comment(r, 60, SIZE, True)
    assert "无 KataGo" in lo["reason"] and "仅参考" in lo["reason"]


def test_low_confidence_must_not_assert_estimate_as_fact():
    """领先/落后的叙述用的是 scoreBefore，启发式引擎给的那个数不可信。"""
    r = report_of((0, 0), (4, 4), 6.0, before_sl=12.0, after_sl=6.0)
    assert "还领先 12.0 目" in commentary.template_comment(r, 60, SIZE, False)["reason"]
    assert "还领先" not in commentary.template_comment(r, 60, SIZE, True)["reason"]


def test_greedy_kind_when_capturing_but_still_bad():
    """提了一堆子仍被判问题手 → 吃小失大，提子数要写出来。"""
    r = report_of((0, 0), (4, 4), 6.0, captures=5)
    assert commentary.diagnose({**r, "totalMoves": 60}, SIZE, False) == commentary.K_GREEDY
    c = commentary.template_comment(r, 60, SIZE)
    assert "提掉了对方 5 子" in c["reason"]
    assert "提掉 5 子" in c["reason"]


def test_size_kind_in_endgame_and_time_kind_when_behind():
    """官子阶段先谈大小；落后 + 缓手 + 距离不近不远 → 时机问题。"""
    r = report_of((0, 0), (4, 4), 6.0, move_num=50)
    assert commentary.diagnose({**r, "totalMoves": 60}, SIZE, False) == commentary.K_SIZE
    c = commentary.template_comment(r, 60, SIZE)
    assert c["reason"].startswith("（官子阶段）")
    assert "先手官子" in c["advice"]

    t = report_of((4, 4), (7, 7), 2.5, move_num=30, before_sl=-9.0, after_sl=-11.0)
    assert commentary.diagnose({**t, "totalMoves": 60}, SIZE, False) == commentary.K_TIME
    tc = commentary.template_comment(t, 60, SIZE)
    assert "抢手" in tc["reason"] and "落后 9.0 目" in tc["reason"]


def test_template_comment_cites_candidate_rank():
    """落点在候选里排第几要如实说；不在候选里时绝不能编出排名。"""
    r = report_of((0, 0), (4, 4), 6.0)
    assert r["playerRank"] == 1
    assert "排第 2" in commentary.template_comment(r, 60, SIZE)["reason"]
    assert "没进入引擎考虑的前列" in \
        commentary.template_comment({**r, "playerRank": None}, 60, SIZE)["reason"]


def test_template_comment_skips_non_problem_moves_and_keeps_contract():
    """好手/虚手/Pass 不产文本；产了的必须带齐前端要用的三个键。"""
    r = report_of((0, 0), (4, 4), 6.0)
    for flag in (FLAG_GOOD, FLAG_PASS):
        assert commentary.template_comment({**r, "flag": flag}, 60, SIZE) == {}
    c = commentary.template_comment(r, 60, SIZE)
    assert set(c) >= {"reason", "advice", "maxim"}
    assert all(isinstance(v, str) and v for v in c.values())


def test_kind_breakdown_counts_only_player_moves():
    """把 AI 的失误算进学生头上是静默错误：误区计数只认玩家的手。"""
    mine = {**report_of((0, 0), (4, 4), 6.0), "isPlayer": True}
    ai = {**report_of((2, 2), (7, 7), 8.0, move_num=6), "isPlayer": False}
    bd = commentary.kind_breakdown([mine, ai], SIZE, False)
    assert sum(bd.values()) == 1 and bd[commentary.K_DIR] == 1


def test_ai_moves_are_described_in_third_person():
    """AI 的问题手也拿得到讲解，而 scoreBefore/scoreAfter 是走子方视角：
    一律说「你」会把 AI 的形势写成玩家的形势（实测踩过：白方的那手被写成
    「走这手之前你还领先 4.8 目」）。这里只改人称相关的两个字段，
    正是要测 template_comment 怎么用它们。
    """
    base = report_of((0, 0), (4, 4), 6.0, before_sl=12.0, after_sl=6.0)
    assert base["isPlayer"] is True
    assert "你还领先 12.0 目" in commentary.template_comment(base, 60, SIZE)["reason"]
    ai = {**base, "isPlayer": False, "color": WHITE}
    c = commentary.template_comment(ai, 60, SIZE)
    assert "白方还领先 12.0 目" in c["reason"]
    assert "你还领先" not in c["reason"]
    assert "白方的落点在引擎候选里排第" in c["reason"]
    # 对学生的建议不能套在 AI 的手上，换成提醒学生去读对方意图
    assert "正是你该提前破坏的地方" in c["advice"]
    assert "先问自己" not in c["advice"]
    assert "离白方这手 4 路" in c["advice"]      # 距离那句也跟着人称
    assert "离你这手" not in c["advice"]
    # 两种人称都要以句号收尾（结语文本拼接处最容易漏）
    assert c["advice"].endswith("。")
    assert commentary.template_comment(base, 60, SIZE)["advice"].endswith("。")


def test_template_texts_do_not_leak_markdown_markers():
    """前端是纯文本渲染，Markdown 星号会原样显示成字符。扫全部话术池。"""
    pools = [list(commentary._KIND_REASON.values()),
             list(commentary._ADVICE_TAIL.values()),
             list(commentary._KIND_LABEL.values())]
    pools += [v for v in commentary._MAXIMS.values()]
    seen = 0
    for pool in pools:
        for v in pool:
            items = v if isinstance(v, list) else [v]
            for t in items:
                seen += 1
                assert "**" not in t, f"话术里混进了 Markdown 星号：{t[:24]}"
    assert seen >= 25, f"只扫到 {seen} 条话术，池子大概没遍历全"


def test_pass_move_without_coordinates_does_not_break_diagnosis():
    """虚手没有落点，却仍可能带 lossPoints（同节点探不到 → 退回跳节点差值）。

    实测踩到的崩溃：确认终局时双方各一手虚手进了阶段统计，取「这一段最差一手」
    时拿到 (None, None) 去算距离，`int(None)` 抛 TypeError 把整个复盘弄成 failed。
    挡的位置必须是 `_pt_ok`（判整个元组是不是 None 不够），因为虚手传的是非空元组。
    """
    rep = report_of((0, 0), (4, 4), 6.0)
    pas = {**rep, "flag": FLAG_PASS, "x": None, "y": None, "gtp": "pass",
           "isPlayer": True, "moveNum": 5}
    assert commentary.diagnose({**pas, "totalMoves": 60}, SIZE, False) == commentary.K_GEN
    assert commentary._gap((None, None), (4, 4)) is None
    assert commentary._bearing((None, None), (4, 4), 9) == ""
    phases = {"opening": phase_of_stats(6.0, 5, 6.0, 3, 18.0),
              "middle": {}, "endgame": {}}
    counts = {"good": 5, "slow": 0, "bad": 0, "blunder": 0, "pass": 3}
    s = commentary.template_summary(ctx_of(), phases, counts, 6.0, 1.0, reports=[pas])
    assert s["overall"] and s["opening"]


# ---- 模板总结 ----------------------------------------------------------------
def test_template_summary_states_accuracy_caliber():
    """评语必须说明吻合度基准口径：最强引擎最优解、与对局页提示同标尺。

    现场问题：提示曾经走档位 human 风格模型，玩家「完全按推荐下」复盘仍被报
    大损失——评语里把尺子说清楚，并指向「跟着提示下就是跟着最优下」。
    """
    s = commentary.template_summary(ctx_of(), phase_of_stats(6.0), {
        "good": 5, "slow": 0, "bad": 0, "blunder": 0, "pass": 3}, 6.0, 1.0, reports=[])
    assert "最强引擎" in s["overall"], s["overall"]
    assert "提示" in s["overall"], s["overall"]


def ctx_of(**over) -> dict:
    ctx = {"rankName": "10级", "aiName": "围棋少年（5级）", "engine": "katago",
           "size": 9, "komi": 5.5, "totalMoves": 60, "resultText": "黑中盘胜",
           "playerColorName": "黑"}
    ctx.update(over)
    return ctx


def phase_of_stats(avg, worst_num=None, worst_loss=None, moves=20, total_loss=0.0) -> dict:
    return {"moves": moves, "avgLoss": avg, "totalLoss": total_loss,
            "worstMoveNum": worst_num, "worstLoss": worst_loss}


def test_template_summary_names_dominant_misconception_and_pivot():
    """总结要说出「你反复错在哪一类」与「胜负在哪一手分的」，而不是只报统计。"""
    r1 = {**report_of((0, 0), (4, 4), 6.0), "isPlayer": True, "moveNum": 5}
    r2 = {**report_of((2, 2), (8, 8), 12.0), "isPlayer": True, "moveNum": 7}
    phases = {"opening": phase_of_stats(3.4, 5, 6.0, 20, 68.0),
              "middle": {}, "endgame": {}}
    counts = {"good": 18, "slow": 1, "bad": 1, "blunder": 0, "pass": 0}
    moments = [{"moveNum": 5, "color": BLACK, "isPlayer": True, "gtp": "A1", "swing": -0.2}]
    s = commentary.template_summary(ctx_of(), phases, counts, 3.1, 1.2,
                                    moments=moments, reports=[r1, r2])
    assert "战场方向" in s["overall"] and "2 处" in s["overall"]
    assert "20.0 个百分点" in s["overall"] and "丢分" in s["overall"]
    # 阶段评语里最差那一手要带归类，并给出累计量
    assert "属于战场方向类问题" in s["opening"]
    assert "累计损失 68.0 目" in s["opening"]
    assert s["endgame"].endswith("暂无评语。")
    assert any("战场方向" in t for t in s["training"])


def test_template_summary_pivot_flips_for_white_player():
    """swing 是黑方视角：同一手黑胜率下降，执黑是丢分、执白是得分。"""
    phases = {"opening": phase_of_stats(1.2, 5, 6.0, 20, 24.0),
              "middle": {}, "endgame": {}}
    counts = {"good": 20, "slow": 1, "bad": 1, "blunder": 0, "pass": 0}
    r = {**report_of((0, 0), (4, 4), 6.0), "isPlayer": True, "moveNum": 5}
    moment = [{"moveNum": 5, "color": BLACK, "isPlayer": True, "gtp": "A1", "swing": -0.2}]
    black = commentary.template_summary(ctx_of(), phases, counts, 2.0, 1.0,
                                        moments=moment, reports=[r])
    white = commentary.template_summary(ctx_of(playerColorName="白"), phases, counts,
                                        2.0, 1.0, moments=moment, reports=[r])
    assert "丢分" in black["overall"] and "得分" not in black["overall"]
    assert "得分" in white["overall"] and "丢分" not in white["overall"]
    assert "降 20.0 个百分点" in white["overall"]      # 黑方胜率降这个事实不变


def test_dan_rank_gets_tighter_phase_bars():
    """同一个「平均每手 0.8 目」：级位学生是表现好，段位学生只是平稳。"""
    phases = {"opening": phase_of_stats(0.8, moves=10, total_loss=8.0),
              "middle": {}, "endgame": {}}
    counts = {"good": 10, "slow": 0, "bad": 0, "blunder": 0, "pass": 0}
    kyu = commentary.template_summary(ctx_of(rankName="10级"), phases, counts, 0.8, None)
    dan = commentary.template_summary(ctx_of(rankName="业余2段"), phases, counts, 0.8, None)
    assert "几乎没有明显损失" in kyu["opening"]
    assert "几乎没有明显损失" not in dan["opening"]
    assert kyu["opening"] != dan["opening"]


def test_template_summary_low_confidence_says_what_is_still_trustworthy():
    """未装 KataGo 时不能只说「仅供参考」就完事：得讲清哪些量仍然可用。"""
    phases = {"opening": phase_of_stats(4.0, 5, 12.0, 10, 40.0),
              "middle": {}, "endgame": {}}
    counts = {"good": 8, "slow": 2, "bad": 1, "blunder": 1, "pass": 0}
    s = commentary.template_summary(ctx_of(engine="heuristic"), phases, counts,
                                    4.2, 3.1)
    assert "启发式引擎" in s["overall"]
    assert "提子数" in s["overall"] and "候选排名" in s["overall"]
    # 吻合度在这种口径下没意义，不能拿来给人定级别
    assert "属于「" not in s["overall"]


def test_template_summary_keeps_frontend_contract():
    """前端直接渲染这几个键，改文本可以，改结构不行。"""
    phases = {"opening": phase_of_stats(1.2, moves=10), "middle": {}, "endgame": {}}
    counts = {"good": 10, "slow": 1, "bad": 0, "blunder": 0, "pass": 0}
    r = {**report_of((0, 0), (4, 4), 6.0), "isPlayer": True}
    s = commentary.template_summary(ctx_of(), phases, counts, 2.0, 1.0, reports=[r])
    for key in ("opening", "middle", "endgame", "overall", "training", "maxim"):
        assert key in s, f"前端要用的 {key} 丢了"
    assert isinstance(s["training"], list) and s["training"] and len(s["training"]) <= 5
    assert all(isinstance(t, str) and t.strip() for t in s["training"])
    assert isinstance(s["maxim"], str) and s["maxim"]
    # 所有阶段都要有文本（哪怕是「数据不足」），否则前端的 kv 表会空一块
    assert all(s[k] for k in ("opening", "middle", "endgame", "overall"))


def test_template_summary_tolerates_legacy_call_signature():
    """不传 moments/reports 的旧调用（以及其他可能的调用方）不能崩。"""
    phases = {"opening": phase_of_stats(1.2, 5, 6.0, 10, 12.0),
              "middle": {}, "endgame": {}}
    counts = {"good": 10, "slow": 1, "bad": 0, "blunder": 0, "pass": 0}
    s = commentary.template_summary(ctx_of(), phases, counts, 1.2, 0.9)
    assert s["overall"] and s["training"] and s["maxim"]


# ---------------------------------------------------------------------------
# 复盘 worker 的进程级状态（桌面端会在同一进程里重启后端）
# ---------------------------------------------------------------------------
def test_the_review_worker_survives_a_second_event_loop(monkeypatch):
    """同进程里第二次起后端，复盘不能静默丢失。

    桌面端把后端跑在一个线程里，`worker` 的队列/任务是模块级单例；重启服务
    （看门狗自愈、换端口）会换一个事件循环复用同一个对象。Py3.12 的
    `asyncio.Queue` 在**首次 get 时**绑死所在循环：第二个循环里的 worker 一
    await 就抛 `RuntimeError: ... is bound to a different event loop` 当场死掉，
    而 `put_nowait()` 不碰那个绑定、照样成功 —— 于是日志写着「已加入复盘队列」，
    DB 里的 `review_status` 却永远停在 `none`，没有任何地方报错。
    （现场：桌面全量跑 —— 单跑 e2e 是绿的，因为它前面没有第二个后端。）
    """
    from app.review import worker

    seen: list[str] = []

    async def fake_run(game_id: str) -> None:
        seen.append(game_id)

    monkeypatch.setattr(worker, "run_review", fake_run)

    async def one_round(gid: str) -> None:
        await worker.start_worker()
        # 先让 worker 跑起来、空等在队列上 —— 真服务里就是这个形态：启动时队列是空的，
        # 对局结束是几十分钟后的事。（不等就直接入队会漏掉这个坑：`get()` 发现队列
        # 非空就直接 `get_nowait()`，根本不碰那个循环绑定，于是第一条能处理完、
        # worker 紧接着就死 —— 首跑就是这样被它骗过去的。）
        await asyncio.sleep(0.05)
        assert worker._worker_task is not None and not worker._worker_task.done(), (
            f"{gid}：复盘 worker 一启动就死了（它复用了绑在别的循环上的队列）")
        worker.enqueue_review(gid)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if seen and seen[-1] == gid:
                break
        await worker.stop_worker()

    for gid in ("game-in-loop-1", "game-in-loop-2"):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(one_round(gid))
        finally:
            loop.close()
    assert seen == ["game-in-loop-1", "game-in-loop-2"], (
        f"第二轮事件循环里复盘没被取走（只看到 {seen}）—— 队列被首个循环绑死了")
