"""死活题：判定引擎、内置题库自洽性、练习接口。

底线是「题目的正解必须由搜索证明，而不是靠人对答案的记忆」。所以这里不只测接口
能不能跑，还把整个内置题库逐题重验一遍：
  * 每条正解线走完必须达成题目目标；
  * 每条失败线走完必须达不成目标；
  * 搜索结论必须对得上**教材口径**：基本形的净活/净死、劫形的「守先活攻先劫」、
    对杀的比气口诀。这三类锚点一旦被破坏，学员看到的就是错答案——比功能缺失严重得多。

题库有一百多题、全量重算要两分多钟，所以这里统一走 library_problems（命中磁盘缓存），
并另有一条测试钉住「入库的缓存必须与源码指纹一致」。
"""
from __future__ import annotations

import time
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.game.rules import BLACK, Board, EMPTY, WHITE, from_gtp, other, to_gtp
from app.main import app
from app.models import TsumegoProblem, TsumegoProgress
from app.tsumego.library import (DEPTH, GOAL_CAPTURE, GOAL_KILL, GOAL_KO_KILL,
                                 GOAL_KO_LIVE, GOAL_LIVE, GOAL_RACE, KIND_CAPTURE,
                                 KIND_CONNECT, KIND_KO, KIND_LIFE, KIND_RACE,
                                 SHAPES, Shape, Spec, TIERS, build_problem,
                                 build_setup, grade_difficulty, make_oracle,
                                 probe_verdict, tier_of)
from app.tsumego.solve import (ALIVE, DEAD, all_points, clone, connect_oracle,
                               connected, enclosed_space, eye_regions,
                               goal_achieved, ld_search, verdict_text)
from app.tsumego.store import library_problems, problem_board


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def library():
    """内置题库（走磁盘缓存）。模块级 fixture：全库只取一次。"""
    return library_problems()


@pytest.fixture()
def auth(client):
    username = f"tsumego{int(time.time() * 1000) % 100000}"
    r = client.post("/api/auth/register", json={
        "username": username, "password": "secret123", "displayName": "死活练习"})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['token']}"}, data["user"]


def _shape(key: str) -> Shape:
    for s in SHAPES:
        if s.key == key:
            return s
    raise KeyError(key)


def _replay(board, to_move: int, moves):
    """按「玩家手 / 对手应手」交替回放，返回 (终局棋盘, 轮到谁)。

    [-1, -1] 是脱先哨兵（L3）：不落子，但著法权照常翻转。
    """
    b = clone(board)
    color = to_move
    for m in moves:
        if int(m[0]) == -1:
            color = other(color)
            continue
        b.play(color, (int(m[0]), int(m[1])))
        color = other(color)
    return b, color


def _spec_of(p: dict) -> Spec:
    """题库字典 → Spec，以便用**出题时的同一套判据**重验。

    不能直接拿 ld_search 重验：它默认用死活判据，而对杀题的目标是「先把对方
    提光」、吃子题的目标是「提掉标记的子」，用错判据会把对的答案算成错的。
    """
    return Spec(
        pid=p["pid"], title=p["title"], kind=p["kind"], goal=p["goal"], size=p["size"],
        setup=[(int(x), int(y), int(c)) for x, y, c in p["setup"]],
        area=[(int(x), int(y)) for x, y in p["space"]],
        player=p["toMove"], protagonist=p["victim"], difficulty=p["difficulty"],
        family=p["family"], hint="", note="",
        targets=[(int(x), int(y)) for x, y in p["targets"]],
        own=[(int(x), int(y)) for x, y in p["own"]],
    )


def _verdict_of(p: dict, board, to_move: int) -> str:
    spec = _spec_of(p)
    oracle = make_oracle(spec, problem_board(p["setup"], p["size"]))
    # 深度与出题保持一致（ld_search 默认 24，出题用 DEPTH=28）：
    # 浅了会在劫形上给出与题库不同的结论，重验就变成假警报。
    return ld_search(board, p["victim"], to_move, spec.area,
                     depth=DEPTH, oracle=oracle)[0]


# ---------------------------------------------------------------- 判定引擎


def test_solver_judges_textbook_straight_three():
    """边上直三：黑先正中做活，白先正中点死，走两端都不行。"""
    shape = _shape("straight-three-edge")
    _, board = build_setup(shape)
    area = enclosed_space(board, BLACK)
    center = (2, 0)                       # C1
    assert center in area

    verdict, pv = ld_search(board, BLACK, BLACK, area)
    assert verdict == "alive", "黑先应当能活"
    assert pv and pv[0] == center, f"急所必须是正中，实际 {pv}"

    verdict, pv = ld_search(board, BLACK, WHITE, area)
    assert verdict == "dead", "白先应当能杀"
    assert pv and pv[0] == center

    # 黑走端点（B1）→ 白点后黑死
    nb = clone(board)
    nb.play(BLACK, (1, 0))
    assert ld_search(nb, BLACK, WHITE, area)[0] == "dead"
    # 黑走正中 → 两个独立眼位
    nb = clone(board)
    nb.play(BLACK, center)
    assert len(eye_regions(nb, BLACK)) >= 2
    assert verdict_text("alive", BLACK) == "黑棋已活（两个独立眼位）"


def test_solver_judges_ko_shapes_like_textbook():
    """劫形按教材口径判定：角上板六 / 盘角曲四 / 角上曲六 = 守先净活、攻先成劫。

    这是四值搜索（DEAD < KO < SEKI < ALIVE）最要紧的锚点：旧实现只有活/死两值，
    任何涉劫形状只能整题丢弃；现在「劫」是一等结论，而且攻方既能净杀又能打劫时
    会选净杀（0 < 1），守方既能净活又只能打劫时会选净活。
    """
    for key in ("corner-six", "corner-bent-four", "corner-l-six"):
        shape = _shape(key)
        _, board = build_setup(shape)
        area = enclosed_space(board, shape.victim)
        assert ld_search(board, shape.victim, shape.victim, area)[0] == "alive", \
            f"{shape.title}：守先应净活"
        verdict, pv = ld_search(board, shape.victim, other(shape.victim), area)
        assert verdict == "ko", f"{shape.title}：攻先应成劫，实际 {verdict}"
        assert pv, "劫形要给出变化图，否则学员看不到劫在哪里"


def test_solver_does_not_turn_plain_shapes_into_ko():
    """对照组：直三/弯三是净活净死，方四两边都是死——不能被劫的模型带跑偏。"""
    for key in ("straight-three-corner", "bent-three-corner"):
        shape = _shape(key)
        _, board = build_setup(shape)
        area = enclosed_space(board, shape.victim)
        assert ld_search(board, shape.victim, shape.victim, area)[0] == "alive", key
        assert ld_search(board, shape.victim, other(shape.victim), area)[0] == "dead", key

    # 方四自己先走也活不了（两个直三区域不等于活：对方点中间后要连补两手）
    square = Shape("square-four-probe", "方四", [(0, 0), (1, 0), (0, 1), (1, 1)],
                   2, "角上", "", "")
    _, board = build_setup(square)
    area = enclosed_space(board, BLACK)
    assert ld_search(board, BLACK, BLACK, area)[0] == "dead", "方四守先也是死"
    assert ld_search(board, BLACK, WHITE, area)[0] == "dead", "方四攻先也是死"
    with pytest.raises(ValueError):
        build_problem(square, GOAL_LIVE)          # 闸门 2：自己先走也达不成目标 → 不出题


def test_connect_oracle_semantics():
    """连接判据的语义（目前没有题型用它，但它是「题型 = 判据」里预留的一块）。"""
    oracle = connect_oracle(BLACK, (0, 0), (2, 0))
    board = Board(9)
    board.place(BLACK, [(0, 0)])
    assert oracle.evaluate(board, frozenset()) == DEAD, "一头不在盘上就是失败"
    board.place(BLACK, [(2, 0)])
    assert oracle.evaluate(board, frozenset()) is None, "两头都在但没连上，还没分胜负"
    board.place(BLACK, [(1, 0)])
    assert oracle.evaluate(board, frozenset()) == ALIVE, "连成一块就是成功"


def test_race_verdicts_match_textbook_counting_mnemonic():
    """对杀搜索结论与教材比气口诀逐条对账（无公气）。

    口诀：气多者胜；气相同则先走者胜；气少先走也输（搜索给 dead，于是不会出题）。
    这条测试把“搜索”与“教材”钉在一起：封压墙、搜索范围、判据任一环节错了，
    先走方就会靠“白得一次安全 pass”把气少的一方抬成胜者，这里第一个报警。
    """
    from app.tsumego.puzzles import _race_spec

    checked = 0
    for b in range(1, 4):
        for w in range(1, 4):
            for rows in (1, 2):
                for player in (BLACK, WHITE):
                    spec = _race_spec(b, w, rows, player)
                    if spec is None:
                        continue
                    own = b if player == BLACK else w
                    theirs = w if player == BLACK else b
                    want = "alive" if own >= theirs else "dead"
                    who = "黑" if player == BLACK else "白"
                    assert probe_verdict(spec) == want, \
                        f"外气 黑{b}:白{w} 块高{rows} {who}先，口诀应为 {want}"
                    checked += 1
    assert checked == 36, f"对杀口诀应对 36 组，实际 {checked}"


def test_race_common_law_cap_side_first_always_wins():
    """公气对杀（L5）：盖石与先手同一方时，先手**必活**（72 组全验）。

    这套模板的几何（底边开口的公气列 + 悬空盖石）如果任一环节错了——
    封压漏缝、盖石接上墙、公气点被误判成普通外气——「盖石方先手必活」
    这条硬规律第一个报警。
    """
    from app.tsumego.puzzles import _race_common_spec

    checked = 0
    for b in range(1, 4):
        for w in range(1, 4):
            for k in (1, 2):
                for rows in (1, 2):
                    for me, cap in ((BLACK, BLACK), (WHITE, WHITE)):
                        spec = _race_common_spec(b, w, k, rows, me, cap)
                        assert spec is not None
                        who = "黑" if me == BLACK else "白"
                        assert probe_verdict(spec) == "alive", \
                            f"{spec.title}（盖石={who}）：盖石方先手必须能胜"
                        checked += 1
    assert checked == 72, f"公气对杀盖石方先手应验 72 组，实际 {checked}"


def test_race_common_adversary_first_verdicts_are_pinned():
    """公气对杀（L5）：对方先走的结果**没有一句口诀能概括**
    （盖石颜色与块高会翻转它），用逐组表钉死（A=活 D=死）。

    表按每格 (b, w, k, rows) 顺序展开：b=1..3 → w=1..3 → k=1..2 → rows=1..2，
    每格 36 个字符。这张表是本轮搜索实测生成的（L5），防回归不靠印象；
    任何一格变化都意味着模板几何或搜索口径变了，需要人工复核。
    """
    from app.tsumego.puzzles import _race_common_spec

    table = {
        BLACK: "ADDDAAAAAAAADDDDDDDDAADADDDDDDDDDDDD",
        WHITE: "ADDDDDDDDDDDAAAADDDDDDDDAAAAAADADDDD",
    }
    checked = 0
    for cap, pattern in table.items():
        other = WHITE if cap == BLACK else BLACK
        idx = 0
        for b in range(1, 4):
            for w in range(1, 4):
                for k in (1, 2):
                    for rows in (1, 2):
                        v = probe_verdict(_race_common_spec(b, w, k, rows, other, cap))
                        want = "alive" if pattern[idx] == "A" else "dead"
                        assert v == want, \
                            f"盖石={'黑' if cap == BLACK else '白'} 外气{b}:{w} 公气{k} 块高{rows}：" \
                            f"对方先手应为 {want}，实际 {v}"
                        idx += 1
                        checked += 1
    assert checked == 72, f"公气对杀对账应 72 组，实际 {checked}"


def test_race_common_shipped_set_is_pinned_and_has_teachable_points():
    """公气对杀（L5）出货清单 = 固定 20 题；每题必须带「错误第一手」（有练点）。

    闸门 3 只管「对方先走活不活」，这里再钉一层：正解首手如果覆盖所有空点
    （怎么下都赢），出给学生就是白捡 —— 每一题都要求有可错的点。
    难度自动打分（1~6，含高难），提示文案按正解线首手类型分流，
    至少两种变体都要出现（纯「紧对方外气」与「紧外气/占公气急所」混合）。
    """
    from app.tsumego.puzzles import race_common_specs

    ps = race_common_specs()
    got = {p["pid"] for p in ps}
    expect = {
        "race-1-2-k1-1r-cb-b", "race-1-2-k1-2r-cb-b", "race-1-2-k2-1r-cb-b",
        "race-1-3-k1-1r-cb-b", "race-1-3-k1-2r-cb-b", "race-1-3-k2-1r-cb-b",
        "race-1-3-k2-2r-cb-b",
        "race-2-1-k1-1r-cw-w", "race-2-1-k1-2r-cw-w", "race-2-1-k2-1r-cw-w",
        "race-2-3-k1-1r-cb-b", "race-2-3-k1-2r-cb-b", "race-2-3-k2-2r-cb-b",
        "race-3-1-k1-1r-cw-w", "race-3-1-k1-2r-cw-w", "race-3-1-k2-1r-cw-w",
        "race-3-1-k2-2r-cw-w", "race-3-2-k1-1r-cw-w", "race-3-2-k1-2r-cw-w",
        "race-3-2-k2-2r-cw-w",
    }
    assert got == expect, f"公气对杀出货清单变了：新增 {got - expect} / 缺失 {expect - got}"
    err_without_wrong = [p["pid"] for p in ps
                         if not any(l["result"] == "wrong" for l in p["lines"])]
    assert not err_without_wrong, f"以下题没有可错的点：{err_without_wrong}"
    hints = {p["hint"] for p in ps}
    assert len(hints) >= 2, f"提示文案没有分流：{hints}"


def test_solver_criterion_is_two_independent_eyes():
    """活棋判据：≥2 个「完全被自己包围」的空点区域。"""
    shape = _shape("bent-three-corner")
    _, board = build_setup(shape)
    assert len(eye_regions(board, BLACK)) == 1        # 起手只有一个眼位
    nb = clone(board)
    nb.play(BLACK, (0, 0))                            # 角上弯三的急所 = 角点
    assert len(eye_regions(nb, BLACK)) >= 2


# ---------------------------------------------------------------- 题库自洽


def _libs(board, group) -> set:
    """一组棋子（视为整体）的气。"""
    out = set()
    for p in group:
        for n in board.neighbors(tuple(p)):
            if board.at(n) == EMPTY:
                out.add(n)
    return out - {tuple(p) for p in group}


def _check_precondition(p: dict, board) -> None:
    """摆子前提：每类题都有自己「题目成立」的定义，前提坏了结论就没意义。"""
    area = {(int(x), int(y)) for x, y in p["space"]}
    victim = p["victim"]
    if p["kind"] in (KIND_LIFE, KIND_KO):
        # 受害方恰好只有一个眼位区域，且气全在搜索范围内（否则它能往 area 外逃）。
        # area = 眼位 + 外气点，而外气点贴着攻方子、**不在 eye_regions 里**
        # （eye_regions 只算被受害方完全围住的空区），所以不能断言 eye == area；
        # 正确的关系是 eye ⊆ area，且 area 里多出来的点必须都是受害方的气。
        regions = eye_regions(board, victim)
        assert len(regions) == 1, p["title"]
        eye = set(regions[0])
        assert eye <= area, f"{p['title']} 眼位超出了搜索范围"
        victim_pts = [q for q in all_points(board) if board.at(q) == victim]
        assert victim_pts, f"{p['title']} 盘上没有被判定的棋"
        libs = _libs(board, victim_pts)
        assert libs and libs <= area, f"{p['title']} 受害方有范围外的气"
        assert area - eye <= libs, \
            f"{p['title']} 搜索范围里有多余的空点（既不是眼位也不是气）"
        return
    own = [tuple(q) for q in p["own"]]
    targets = [tuple(q) for q in p["targets"]]
    if p["kind"] == KIND_CONNECT:
        # 连络题：own 是两个端点，开局必须**还没连上**（已经连上就无需动手，
        # 闸门 3 本该拒掉这道题）。
        assert len(own) == 2, f"{p['title']} 连络题要恰好两个端点"
        assert all(board.at(q) == victim for q in own), p["title"]
        assert not connected(board, victim, own[0], own[1]), \
            f"{p['title']} 开局两块棋已经连上了"
        return
    assert targets, f"{p['title']} 缺目标子"
    if p["kind"] == KIND_RACE:
        assert own, f"{p['title']} 对杀题必须标出两块棋"
        assert all(board.at(q) == victim for q in own), p["title"]
        assert all(board.at(q) == other(victim) for q in targets), p["title"]
        # 两块棋的气必须全在搜索范围内：范围外的气对方永远填不到，比气就算不准
        assert _libs(board, own) <= area, f"{p['title']} 己方有范围外的气"
        assert _libs(board, targets) <= area, f"{p['title']} 对方有范围外的气"
    if p["kind"] == KIND_CAPTURE:
        assert all(board.at(q) == other(victim) for q in targets), p["title"]
        assert _libs(board, targets) <= area, f"{p['title']} 目标子有范围外的气，吃不到"


def test_builtin_library_is_self_consistent(library):
    """逐题重验：摆子前提成立、正解达成目标、失败线达不成、范围内每一手都被归类。"""
    assert len(library) >= 100, f"内置题量太少：{len(library)}"

    seen_ids = set()
    for p in library:
        assert p["pid"] not in seen_ids, f"题目 id 重复：{p['pid']}"
        seen_ids.add(p["pid"])
        assert 1 <= p["difficulty"] <= 9
        assert p["kindText"] and p["goalText"] and p["hint"] and p["source"], p["title"]
        board = problem_board(p["setup"], p["size"])
        _check_precondition(p, board)
        area = [(int(x), int(y)) for x, y in p["space"]]
        goal = p["goal"]

        correct = [l for l in p["lines"] if l["result"] == "correct"]
        wrong = [l for l in p["lines"] if l["result"] == "wrong"]
        assert correct, f"{p['title']} 没有正解线"
        legal = [q for q in area
                 if board.at(q) == EMPTY and board.is_legal(p["toMove"], q)]
        assert len(correct) + len(wrong) == len(legal), \
            (f"{p['title']} 范围内每一手合法落子都应被归类："
             f"{len(correct)}+{len(wrong)} vs {len(legal)}")
        if p["kind"] not in (KIND_RACE, KIND_CONNECT):
            # 对杀常有多个等价的紧气顺序；连络（L3）的两个缺口点等价——
            # 这形练的是「看出缺口 + 接不归」，不是独一着手（connect_specs
            # 的 docstring 写明）。其余题型都要求唯一正解
            assert len(correct) == 1, f"{p['title']} 应有唯一正解，实际 {len(correct)}"

        for line in correct:
            final, turn = _replay(board, p["toMove"], line["moves"])
            verdict = _verdict_of(p, final, turn)
            assert goal_achieved(verdict, goal), \
                f"{p['title']} 正解 {line['moves']} 判定为 {verdict}，与目标 {goal} 矛盾"
            assert line["comment"], "正解必须带讲解"
        for line in wrong:
            final, turn = _replay(board, p["toMove"], line["moves"])
            verdict = _verdict_of(p, final, turn)
            assert not goal_achieved(verdict, goal), \
                f"{p['title']} 失败线 {line['moves']} 竟然达成了目标"
            if len(line["moves"]) < 2:
                # 对方无需应手（或局部已无处可下）时变化图可以为空，但讲解必须说清楚
                assert "无需应手" in line["comment"], \
                    f"{p['title']} 失败线没给应对，讲解也没交代原因"


def test_library_covers_kinds_and_goals(library):
    """题库要真的覆盖多个题型（而不是一种题型的多个变体）。"""
    kinds = {p["kind"] for p in library}
    goals = {p["goal"] for p in library}
    assert {KIND_LIFE, KIND_KO, KIND_RACE, KIND_CAPTURE} <= kinds, kinds
    assert {GOAL_LIVE, GOAL_KILL, GOAL_KO_KILL, GOAL_RACE, GOAL_CAPTURE} <= goals, goals
    assert {"角上", "边上", "中腹", "手筋"} <= {p["family"] for p in library}
    for p in library:
        if p["kind"] in (KIND_LIFE, KIND_KO):
            # 做活/劫活：玩家就是被判定的一方；杀棋/劫杀：玩家是攻方
            if p["goal"] in (GOAL_LIVE, GOAL_KO_LIVE):
                assert p["toMove"] == p["victim"], p["title"]
            else:
                assert p["toMove"] == other(p["victim"]), p["title"]
        elif p["kind"] == KIND_CONNECT and p["goal"] == "cut":
            # 切断题是连络题的镜像：主角仍是「想连的那一方」，而玩家是拦它的人
            assert p["toMove"] == other(p["victim"]), p["title"]
        else:
            assert p["toMove"] == p["victim"], \
                f"{p['title']} 对杀/吃子/连络题里玩家就是主角"


def test_outside_liberties_match_textbook_mnemonic():
    """角上板六的外气口诀：「没有外气是死棋，一口外气是打劫，两口外气是活棋」。

    守先（被围的那一方自己走）三档都是净活，分水岭在攻先。0 外气那档的局部
    结论是劫、而口诀说「死棋」，差的是规则层：角上找不到劫材，「劫尽棋亡」。
    搜索只给局部结论，这层区别由题目的 note 讲清楚。
    """
    from app.game.rules import BLACK, WHITE
    from app.tsumego.library import validate_setup

    six = [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)]
    cases = [(0, [], "ko"), (1, [(3, 1)], "ko"), (2, [(3, 1), (3, 2)], "alive")]
    for n, libs, want in cases:
        shape = Shape(key=f"six-L{n}", title=f"角上板六·{n}口外气", space=six,
                      difficulty=4, family="角上", hint="", note="", size=9,
                      victim=BLACK, outside_libs=list(libs))
        setup, board = build_setup(shape)
        validate_setup(shape, board)
        area = sorted(set(enclosed_space(board, BLACK)) | set(libs))
        spec = Spec(pid="t", title=shape.title, kind=KIND_LIFE, goal=GOAL_KILL,
                    size=9, setup=setup, area=area, player=WHITE, protagonist=BLACK,
                    difficulty=4, family="角上", hint="", note="")
        assert probe_verdict(spec) == want, f"角上板六 {n} 口外气：攻先应为 {want}"


def test_outside_liberty_is_sealed_on_the_far_side():
    """外气点背向受害方的一侧必须封死（四邻一个空点也不能剩）。

    不封的话那个空点在 area 外永远有气、谁都吃不掉，等于给先走方一次白得的
    pass（与对杀模板的「中立点」病同源）。实测：不封时角上直三加一口外气会算出假的 seki。
    """
    from app.game.rules import BLACK, EMPTY
    from app.tsumego.library import validate_setup

    shape = Shape(key="t", title="角上直三·1口外气", space=[(0, 0), (1, 0), (2, 0)],
                  difficulty=1, family="角上", hint="", note="", size=9,
                  victim=BLACK, outside_libs=[(4, 0)])
    _, board = build_setup(shape)
    validate_setup(shape, board)
    for lib in shape.outside_libs:
        assert board.at(lib) == EMPTY, "外气点本身必须是空的"
        leaks = [n for n in board.neighbors(lib) if board.at(n) == EMPTY]
        assert not leaks, f"外气点 {lib} 的邻点 {leaks} 没被封住，会成为填不掉的中立点"


def test_difficulty_is_graded_from_solution_length():
    """难度必须由正解线手数推出来，不能手填（手填的数字不可比、也无法验证）。"""
    assert grade_difficulty(3, "dead", 4) < grade_difficulty(9, "dead", 4)
    assert grade_difficulty(5, "ko", 4) > grade_difficulty(5, "dead", 4)      # 劫更难
    assert grade_difficulty(5, "dead", 8) > grade_difficulty(5, "dead", 4)    # 范围大更难
    assert grade_difficulty(5, "dead", 4, solutions=3) < grade_difficulty(5, "dead", 4)
    assert grade_difficulty(1, "dead", 3) == 1        # 下限
    assert grade_difficulty(99, "ko", 9) == 9         # 上限
    assert [tier_of(d) for d in (1, 3, 5, 7, 9)] == ["入门", "初级", "中级", "高级", "段位"]


def test_library_tiers_have_no_gap(library):
    """五个难度档都要有题，而且最深的正解线要真的深。

    后半句是用户抱怨「太简单」的直接量化指标：扩容前全库最深只有 9 手、
    而且 44/76 道吃子题只有 3 手。
    """
    tiers = {p["tier"] for p in library}
    assert {"入门", "初级", "中级", "高级", "段位"} <= tiers, tiers
    deepest = max(1 + len(line.get("pv") or [])
                  for p in library for line in p["lines"] if line["result"] == "correct")
    assert deepest >= 11, f"最深的正解线只有 {deepest} 手，高难题没进来"


def test_library_pids_and_titles_are_unique(library):
    """pid 不能重复，标题也不能重复。

    pid 是入库主键的来源：重复会让后写的覆盖先写的，学员的练习记录跟着错乱。
    标题重复则是可读性事故——store 会给撞名的题加「（1）（2）」后缀，可学员看到
    「角上6目（1）」「角上6目（2）」根本分不清谁是谁。外气轴刚上线时真出过
    15 对撞名，根因是标题只报目数、把板六/曲六/葡萄六的形状身份丢了。
    """
    dup_pid = [k for k, n in Counter(p["pid"] for p in library).items() if n > 1]
    assert not dup_pid, f"pid 重复：{sorted(dup_pid)}"
    dup_title = [k for k, n in Counter(p["title"] for p in library).items() if n > 1]
    assert not dup_title, f"标题重复：{sorted(dup_title)}"


def test_connect_kind_is_shipped_with_visible_lines():
    """连络/切断题型（L3）正式上线：w=2 的正解线必须含脱先哨兵且能演示完。

    此前故意不出题：对方无处可下只能脱先，而真正连通发生在脱先之后——
    pv 的旧口径（_before_pass 截断）把变化图变成空的，答案面板演示不出
    「两块棋连上了」。L3 起 pv 以 [-1,-1] 哨兵保留脱先（solve.TENUKI），
    此行必须验证三件事：
      1. 真有题产出（连络与切断都要有）；
      2. 每条正解线的 pv 非空、且含有脱先哨兵（这正是当初卡住的地方）；
      3. 正解线（含哨兵）在真实棋盘上回放必须把目标演示到底——
         用出题时的同一套判据（connect_oracle）重验最终局面。
    """
    from app.tsumego.puzzles import connect_specs

    ps = connect_specs()
    assert ps, "连络/切断没有产出任何题"
    goals = {p["goal"] for p in ps}
    assert goals >= {"connect", "cut"}, f"连络与切断都要有题：{goals}"
    for p in ps:
        good = [l for l in p["lines"] if l["result"] == "correct"]
        assert good, f"{p['pid']} 没有正解线"
        for line in good:
            assert line["pv"], f"{p['pid']} 正解线没有可见变化（脱先没保留？）"
            assert any(m[0] == -1 for m in line["pv"]), \
                f"{p['pid']} 正解线缺脱先哨兵：{line['pv']}"
            # 正解线走完必须真的把两块棋连上（或切断成立）：
            # 用回放 + connect_oracle 重验，防「样子像题、结论是假的」
            spec = _spec_of(p)
            board = problem_board(p["setup"], p["size"])
            final, _ = _replay(board, p["toMove"], line["moves"] + line["pv"])
            ends = [tuple(q) for q in p["own"]]
            assert connected(final, BLACK, ends[0], ends[1]) == (p["goal"] == "connect"), \
                f"{p['pid']} 正解线走完没有达成目标：{line['moves'] + line['pv']}"


def test_sacrifice_tesuji_is_labelled_snapback():
    """手筋名从事后的变化图归类：正解子被对方提掉 → 倒扑。

    这里钉住的是**归类函数**本身，不是题库里有多少类：吃子枚举目前只能产出
    紧气吃（白棋是实心小块、没有内部空点，黑棋无处可扑），归类逻辑没机会发挥。
    """
    from app.game.rules import BLACK, WHITE
    from app.tsumego.puzzles import _decorate_capture

    # 黑下 (1,1) 只有一口气 (1,0)；白下 (1,0) 就把黑这一子提了 → 弃子手筋
    setup = [(0, 1, WHITE), (2, 1, WHITE), (1, 2, WHITE), (0, 0, BLACK)]
    spec = Spec(pid="t", title="t", kind=KIND_CAPTURE, goal=GOAL_CAPTURE, size=9,
                setup=setup, area=[(1, 1), (1, 0)], player=BLACK, protagonist=BLACK,
                difficulty=3, family="手筋", hint="", note="", targets=[(0, 1)])
    problem = {"title": "旧标题", "tags": [], "lines": [
        {"result": "correct", "moves": [[1, 1]], "pv": [[1, 0]], "comment": "正解"}]}
    _decorate_capture(problem, spec)
    assert problem["title"].startswith("倒扑"), problem["title"]
    assert "弃子" in problem["lines"][0]["comment"]


def test_pocket_capture_problems_ship_with_sacrifice_lines(library):
    """口袋吃子（L4）：白棋带内部空点/假眼位 → 更高一档的杀棋题（题库 414、中级以上 43）。

    钉四件事：
      1. 出货量够（≥50）且难度覆盖 2~4（比旧实心块手筋高一档，难度 5 也出现）；
      2. 每题的正解线非空（能演示——过去的「空变化图」缺陷不可复发）；
      3. 全部归类为「紧气吃」——**首手弃子的经典倒扑在角窗几何下没有生成**
         （§28.2 的分析：弃子需要白块「只有一条活路且活路邻黑环」的精确形状，
         与 area≤12 + 闸门 3 互斥，正是 §9.9-1 的预言）。将来真倒扑模板落地时
         这条断言会红 = 提醒更新分类口径与日志记录（L3 同款提醒式回归）；
      4. 全库自洽测试会逐题回放这些线的全部 plies（形状/搜索口径错当场红）。
    走缓存 fixture（library），不在测试里重跑 166 秒的生成。
    """
    ps = [p for p in library if p["pid"].startswith("pcap-")]
    assert len(ps) >= 50, f"口袋吃子出货太少：{len(ps)}"
    assert {p["difficulty"] for p in ps} >= {2, 3}, \
        f"难度档位没铺开：{[p['difficulty'] for p in ps]}"
    for p in ps:
        good = [l for l in p["lines"] if l["result"] == "correct"]
        assert good and good[0]["pv"], f"{p['pid']} 正解线为空"
    names = {t for p in ps for t in p.get("tags", [])}
    assert "紧气吃" in names, f"口袋模板的归类名没了：{names}"
    assert not (names & {"弃子提", "倒扑"}), \
        "口袋模板开始出弃子手筋了：更新 _decorate_capture 的口径与 §28 的记录"


def test_library_cache_matches_source_fingerprint():
    """本地缓存必须与当前源码指纹一致。

    不一致意味着：改了判定逻辑/形状表，启动时会现场重算两分多钟。
    跑一次 library_problems(force=True) 重新生成即可。缓存文件不入版本库，
    每个部署各自生成。
    """
    from app.tsumego.store import _fingerprint, _load_cache

    assert _load_cache(_fingerprint()) is not None, \
        "题库缓存过期：跑 library_problems(force=True) 重新生成 library_cache.json"


def test_dev_fingerprint_is_exactly_the_source_hash():
    """开发期的指纹必须就是「四个源文件的 sha256」，逐字节算法钉住。

    为什么单独钉这一条：指纹一变，本地 library_cache.json 就作废，而重新生成要
    两分多钟 —— 任何人改指纹算法（包括加打包兼容分支）都会在这里撞红，
    而不是在别人的机器上撞红。上面那条“缓存没过期”配着这条看：一边钉算法，
    一边钉缓存。
    """
    import hashlib
    from pathlib import Path

    from app.tsumego import store

    here = Path(store.__file__).resolve().parent
    digest = hashlib.sha256(f"v{store.CACHE_VERSION}".encode())
    for name in store._CACHE_SOURCES:
        digest.update((here / name).read_bytes())
    assert store._fingerprint() == digest.hexdigest()[:16]


def test_fingerprint_survives_a_frozen_install_without_sources(monkeypatch):
    """模拟打包后的样子：模块旁边没有 .py 源码。

    不是钻出来的担忧：PyInstaller 只往包里放字节码，而 `_fingerprint()` 是
    `library_problems()` 的第一行 —— 一个 FileNotFoundError 会拖死整个出题接口，
    桌面端的表现就是死活练习页永远转圈。现在它必须不抛、稳定，并且**不命中**
    旧缓存（那是重算一次，不是崩）。
    """
    import pathlib
    import re

    from app.tsumego import store

    real = pathlib.Path.read_bytes
    blocked = set(store._CACHE_SOURCES)

    def fake(self):
        if self.name in blocked and self.parent.name == "tsumego":
            raise OSError(2, "No such file or directory")
        return real(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", fake)
    fp = store._fingerprint()                       # 关键：不许抛
    assert re.fullmatch(r"[0-9a-f]{16}", fp), fp
    assert fp == store._fingerprint(), "同一进程里两次指纹不一样，缓存永远不会命中"
    assert store._load_cache(fp) is None, "拿开发期的旧缓存当真是对的题集"


def test_seed_is_idempotent_and_upserts(client):
    from app.tsumego.store import seed_builtin
    n1 = seed_builtin()
    n2 = seed_builtin()
    assert n1 == n2 > 0
    with SessionLocal() as db:
        rows = db.query(TsumegoProblem).filter_by(builtin=True).count()
    assert rows == n1


# ---------------------------------------------------------------- 接口


def _fetch_problem(client, headers, goal=None, kind=None) -> dict:
    params = {}
    if goal:
        params["goal"] = goal
    if kind:
        params["kind"] = kind
    r = client.get("/api/tsumego/next", headers=headers, params=params or None)
    assert r.status_code == 200, r.text
    return r.json()["problem"]


def _answer_of(problem_id: str) -> dict:
    """测试特权：直接从库里取答案（接口是不会下发的）。"""
    with SessionLocal() as db:
        rec = db.get(TsumegoProblem, problem_id)
        assert rec is not None
        return {"lines": list(rec.lines), "toMove": rec.to_move,
                "victim": rec.victim, "goal": rec.goal, "size": rec.size}


def test_problem_list_never_leaks_answers(client, auth):
    headers, _ = auth
    r = client.get("/api/tsumego/problems", headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items, "题库应在启动时落库"
    for it in items:
        for banned in ("lines", "pv", "refutation", "answer", "solution"):
            assert banned not in it, f"题目列表泄漏了答案字段 {banned}"
        assert it["hint"], "每题都该有提示"
        assert it["source"], "每题都该标出处/许可"


def test_attempt_correct_move_solves_and_records_progress(client, auth):
    headers, _ = auth
    problem = _fetch_problem(client, headers, goal=GOAL_LIVE)
    pid = problem["id"]
    ans = _answer_of(pid)
    correct = [l for l in ans["lines"] if l["result"] == "correct"][0]
    move = correct["moves"][0]

    r = client.post(f"/api/tsumego/{pid}/attempt", headers=headers, json={"moves": [move]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "solved", body
    assert body["goalAchieved"] is True
    assert body["verdict"] == "alive"
    assert body["board"][move[1]][move[0]] == problem["toMove"], "回传的棋盘要含玩家这一手"
    assert body["solved"] is True and body["attempts"] == 1

    summary = client.get("/api/tsumego/summary", headers=headers).json()
    assert summary["solved"] >= 1 and summary["attempts"] >= 1
    assert summary["byGoal"][GOAL_LIVE]["solved"] >= 1

    # 再解一次不应把「已解出」变成两次计数以外的东西
    r2 = client.post(f"/api/tsumego/{pid}/attempt", headers=headers, json={"moves": [move]})
    assert r2.json()["status"] == "solved"
    assert r2.json()["attempts"] == 2 and r2.json()["solved"] is True


def test_attempt_wrong_move_returns_refutation(client, auth):
    headers, _ = auth
    problem = _fetch_problem(client, headers, goal=GOAL_KILL)
    pid = problem["id"]
    ans = _answer_of(pid)
    wrong = [l for l in ans["lines"] if l["result"] == "wrong"]
    assert wrong, "杀棋题应有失败线"
    bad = wrong[0]["moves"][0]

    body = client.post(f"/api/tsumego/{pid}/attempt", headers=headers,
                       json={"moves": [bad]}).json()
    assert body["status"] == "failed", body
    assert body["goalAchieved"] is False
    assert body["refutation"], "必须告诉学员对方的最佳应对"
    assert body["verdict"] in ("alive", "dead")
    assert body["solved"] is False and body["attempts"] == 1
    # 失败变化要摆到回传的棋盘上（教学用）
    rx, ry = body["refutation"][0]
    assert body["board"][ry][rx] != 0, "对方的应手应出现在回传棋盘里"


def test_move_outside_eye_space_is_judged_not_ignored(client, auth):
    """脱先到眼位外：不能含糊判错，要用搜索给出真实结论。"""
    headers, _ = auth
    problem = _fetch_problem(client, headers, goal=GOAL_LIVE)
    far = None
    space = {(int(x), int(y)) for x, y in problem["space"]}
    occupied = {(int(s[0]), int(s[1])) for s in problem["setup"]}
    for y in range(problem["size"] - 1, -1, -1):
        for x in range(problem["size"] - 1, -1, -1):
            if (x, y) not in space and (x, y) not in occupied:
                far = [x, y]
                break
        if far:
            break
    assert far, "盘面上应存在眼位外的空点"
    body = client.post(f"/api/tsumego/{problem['id']}/attempt", headers=headers,
                       json={"moves": [far]}).json()
    assert body["status"] == "failed", body
    assert body["verdict"] == "dead", "做活题脱先后仍是死形"
    assert "不成立" in body["comment"]


def test_attempt_reused_verdict_matches_a_fresh_search(client, auth, library):
    """答对时 attempt 直接取变化线里存的结论（性能优化），但它必须与现场重搜一致。

    实测 area=8 的题单次 ld_search 要 ~100ms，而框架 + 数据库只 ~13ms，那次重搜
    占了响应的九成；复用后 attempt 从 108ms 降到 7ms。但复用错就等于给学员看错的
    结论文案，所以逐题对账。

    选题：搜索范围最大的三道（area 越大候选点越多，最容易算出不同结论）
    + 每个题型各一道（死活/对杀/吃子的判据不同，用错口径会在这里露出来）。
    """
    headers, _ = auth
    widest = sorted(library, key=lambda p: -len(p["space"]))[:3]
    per_kind: dict = {}
    for p in library:
        per_kind.setdefault(p["kind"], p)
    picks = {p["pid"]: p for p in (*widest, *per_kind.values())}
    assert len(picks) >= 4, "至少要覆盖到四个题型"
    for p in picks.values():
        line = next(l for l in p["lines"] if l["result"] == "correct")
        # 复用的前提：玩家只下一手，此时 attempt 的 board 恰好等于 setup + 这一手，
        # 与出题时搜索的局面完全相同。多手线的 board 里还含着对手应手，局面已不同
        # （attempt 里对多手线会自动退回现场搜索，这条断言则是提醒重新评估那个优化）。
        assert len(line["moves"][0::2]) == 1, f"{p['title']} 正解线不是单手，复用前提变了"
        move = line["moves"][0]
        r = client.post(f"/api/tsumego/{p['pid']}/attempt", headers=headers,
                        json={"moves": [move]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "solved", (p["title"], body["status"], body["comment"])
        assert body["goalAchieved"] is True, p["title"]
        # 现场重算一遍：setup + 玩家这一手，轮到对方
        board = problem_board(p["setup"], p["size"])
        board.play(p["toMove"], (int(move[0]), int(move[1])))
        fresh = _verdict_of(p, board, other(p["toMove"]))
        assert body["verdict"] == fresh, \
            f"{p['title']}：接口给的 {body['verdict']} 与现场重搜 {fresh} 不一致"
        assert line["verdict"] == fresh, f"{p['title']}：题库里存的结论就已经不对"
        assert body["verdictText"], f"{p['title']} 结论要有人读文案"


def test_attempt_rejects_illegal_point(client, auth):
    headers, _ = auth
    problem = _fetch_problem(client, headers)
    occupied = problem["setup"][0]
    r = client.post(f"/api/tsumego/{problem['id']}/attempt", headers=headers,
                    json={"moves": [[occupied[0], occupied[1]]]})
    assert r.status_code == 400
    assert "已有棋子" in r.json()["detail"] or "非法" in r.json()["detail"]


def test_solution_returns_lines_and_marks_seen_answer(client, auth):
    headers, _ = auth
    problem = _fetch_problem(client, headers)
    pid = problem["id"]
    body = client.get(f"/api/tsumego/{pid}/solution", headers=headers).json()
    assert body["lines"], "看答案要返回变化线"
    assert any(l["result"] == "correct" for l in body["lines"])
    assert all(l["movesText"] for l in body["lines"]), "变化线要给人读的坐标"
    assert body["source"]
    # 答案面板要显示「这道题多难」，难度与档位都得下发，且两者要自洽
    assert body["tier"] == tier_of(body["difficulty"]), body.get("title")

    item = {i["id"]: i for i in
            client.get("/api/tsumego/problems", headers=headers).json()["items"]}[pid]
    assert item["seenAnswer"] is True
    assert item["solved"] is False, "看答案不等于解出"
    assert item["attempts"] >= 1


def test_progress_is_per_user(client, auth):
    headers, _ = auth
    problem = _fetch_problem(client, headers, goal=GOAL_LIVE)
    pid = problem["id"]
    ans = _answer_of(pid)
    move = [l for l in ans["lines"] if l["result"] == "correct"][0]["moves"][0]
    assert client.post(f"/api/tsumego/{pid}/attempt", headers=headers,
                       json={"moves": [move]}).json()["status"] == "solved"

    other_user = {"Authorization": "Bearer " + client.post("/api/auth/register", json={
        "username": f"peer{int(time.time() * 1000) % 100000}",
        "password": "secret123"}).json()["token"]}
    items = {i["id"]: i for i in
             client.get("/api/tsumego/problems", headers=other_user).json()["items"]}
    assert items[pid]["solved"] is False, "别人的进度不能串到我这里"
    assert client.get("/api/tsumego/summary", headers=other_user).json()["solved"] == 0


def test_next_prefers_unsolved(client, auth):
    headers, _ = auth
    first = _fetch_problem(client, headers)["id"]
    ans = _answer_of(first)
    move = [l for l in ans["lines"] if l["result"] == "correct"][0]["moves"][0]
    client.post(f"/api/tsumego/{first}/attempt", headers=headers, json={"moves": [move]})
    second = _fetch_problem(client, headers)["id"]
    assert second != first, "已解出的题不应立刻再出"


def test_filters_and_404(client, auth):
    headers, _ = auth
    r = client.get("/api/tsumego/problems", headers=headers, params={"goal": "kill"})
    assert r.status_code == 200
    assert all(i["goal"] == "kill" for i in r.json()["items"])
    assert client.get("/api/tsumego/problems", headers=headers,
                      params={"goal": "bad"}).status_code == 422
    assert client.get("/api/tsumego/nope", headers=headers).status_code == 404
    assert client.post("/api/tsumego/nope/attempt", headers=headers,
                       json={"moves": [[0, 0]]}).status_code == 404
    assert client.get("/api/tsumego/next", headers=headers,
                      params={"difficulty": 99}).status_code == 422
    # 难度档位筛选：学员想的是「做中级的」而不是「做难度 5 的」，所以按档筛是主路径
    body = client.get("/api/tsumego/problems", headers=headers,
                      params={"tier": "段位"}).json()
    assert body["items"], "段位档应该有题（test_library_tiers_have_no_gap 已钉住）"
    assert all(i["tier"] == "段位" for i in body["items"])
    assert client.get("/api/tsumego/next", headers=headers,
                      params={"tier": "段位"}).json()["problem"]["tier"] == "段位", \
        "列表与出题两个接口的档位过滤必须同源"
    assert client.get("/api/tsumego/problems", headers=headers,
                      params={"tier": "职业"}).status_code == 422, "非法档位要当场拒"
    # 需要登录
    assert client.get("/api/tsumego/problems").status_code in (401, 403)


def test_problem_list_exposes_kind_and_targets(client, auth):
    """前端要靠 kind / targets / own 画题型标签与目标子标记，这几个字段必须下发。"""
    headers, _ = auth
    items = client.get("/api/tsumego/problems", headers=headers).json()["items"]
    assert {KIND_LIFE, KIND_KO, KIND_RACE, KIND_CAPTURE} <= {i["kind"] for i in items}
    setup_of = {i["id"]: [list(s) for s in i["setup"]] for i in items}
    for it in items:
        assert it["kindText"] and it["goalText"], it["title"]
        # 档位必须由难度数字推出来：两者不自洽就说明有一处是手填的
        assert it["tier"] == tier_of(it["difficulty"]), it["title"]
        assert it["goalText"] != it["goal"], "目标要给人读的中文，不是枚举值"
        if it["kind"] == KIND_CAPTURE:
            assert it["targets"], "吃子题必须标出目标子"
            for tx, ty in it["targets"]:
                assert [tx, ty, other(it["toMove"])] in setup_of[it["id"]], \
                    f"{it['title']} 目标子不在摆子里"
        if it["kind"] == KIND_RACE:
            assert it["own"] and it["targets"], "对杀题要把两块棋都标出来"
    r = client.get("/api/tsumego/problems", headers=headers, params={"kind": KIND_RACE})
    assert r.status_code == 200
    assert r.json()["items"] and all(i["kind"] == KIND_RACE for i in r.json()["items"])
    assert client.get("/api/tsumego/problems", headers=headers,
                      params={"kind": "nope"}).status_code == 422
    assert client.get("/api/tsumego/next", headers=headers,
                      params={"goal": GOAL_KO_KILL}).status_code == 200


def test_race_and_capture_problems_are_playable(client, auth):
    """新题型全链路：出题 → 正解判定 → 结论文案按题型走（不能拿死活的口径说对杀）。

    L3 起 KIND_CONNECT 也走这条链：连络 = alive、切断 = seki（达成值见
    solve.GOALS——切断的结局是占死缺口的僵局，不是提掉对方）。
    """
    headers, _ = auth
    expect_verdict = {KIND_RACE: "alive", KIND_CAPTURE: "alive",
                      KIND_CONNECT: {"connect": "alive", "cut": "seki"}}
    for kind in (KIND_RACE, KIND_CAPTURE, KIND_CONNECT):
        problem = _fetch_problem(client, headers, kind=kind)
        assert problem["kind"] == kind
        ans = _answer_of(problem["id"])
        correct = [l for l in ans["lines"] if l["result"] == "correct"]
        assert correct, f"{problem['pid']} 没有正解线"
        # 连络/切断有两条等价正解（两个缺口点都行），任取其一
        move = correct[0]["moves"][0]
        body = client.post(f"/api/tsumego/{problem['id']}/attempt", headers=headers,
                           json={"moves": [move]}).json()
        assert body["status"] == "solved", (kind, body)
        assert body["goalAchieved"] is True, body
        want = expect_verdict[kind]
        if isinstance(want, dict):
            want = want[problem["goal"]]
        assert body["verdict"] == want, (kind, body)
        assert "眼位" not in body["verdictText"], \
            f"{kind} 题不能拿死活的文案：{body['verdictText']}"

        bad = [l for l in ans["lines"] if l["result"] == "wrong"]
        if bad:
            # 连络/切断的搜索范围 = 一线缺口（两个点都是正解），天然没有错解线——
            # 这形练的是「看出缺口与接不归」，不是独一着手；错解分支只对
            # 有可错下法的题型验（对杀/吃子）。
            body2 = client.post(f"/api/tsumego/{problem['id']}/attempt", headers=headers,
                                json={"moves": [bad[0]["moves"][0]]}).json()
            assert body2["status"] == "failed", (kind, body2)
            assert body2["goalAchieved"] is False, body2


def test_ko_problem_tells_the_student_it_is_a_ko(client, auth):
    """劫题的结论要能说出「劫」，而不是含糊地归到活/死两值里。"""
    headers, _ = auth
    problem = _fetch_problem(client, headers, kind=KIND_KO)
    assert problem["goal"] == GOAL_KO_KILL
    ans = _answer_of(problem["id"])
    wrong = [l for l in ans["lines"] if l["result"] == "wrong"]
    assert wrong, "劫题应有失败线（点错地方就杀不掉）"
    body = client.post(f"/api/tsumego/{problem['id']}/attempt", headers=headers,
                       json={"moves": [wrong[0]["moves"][0]]}).json()
    assert body["status"] == "failed", body
    assert body["goalAchieved"] is False
    assert body["verdict"] != "ko", "点错位置就不应再是劫（否则就是另一个正解）"

    move = [l for l in ans["lines"] if l["result"] == "correct"][0]["moves"][0]
    body2 = client.post(f"/api/tsumego/{problem['id']}/attempt", headers=headers,
                        json={"moves": [move]}).json()
    assert body2["status"] == "solved", body2
    assert body2["verdict"] == "ko", "正解的结论就是劫"
    assert "劫" in body2["verdictText"], body2["verdictText"]
    assert body2["goalAchieved"] is True


def test_summary_breaks_down_by_kind(client, auth):
    headers, _ = auth
    body = client.get("/api/tsumego/summary", headers=headers).json()
    assert {KIND_LIFE, KIND_KO, KIND_RACE, KIND_CAPTURE} <= set(body["byKind"])
    assert sum(v["total"] for v in body["byKind"].values()) == body["total"]
    assert body["byKind"][KIND_RACE]["text"] == "对杀"
    # 档位分组：顺序必须是 TIERS 的顺序（前端直接渲染不再排），总数与 byKind 同源
    assert list(body["byTier"]) == [name for _, name in TIERS], list(body["byTier"])
    assert sum(v["total"] for v in body["byTier"].values()) == body["total"]


def test_coordinates_match_gtp_convention(library):
    """题库坐标必须与全局约定一致（y=0 是 GTP 第 1 行），否则答案会整体上下翻转。"""
    p = next(x for x in library if x["title"].startswith("角上直三"))
    assert to_gtp((0, 0), 9) == "A1"
    assert from_gtp("A1", 9) == (0, 0)
    correct = [l for l in p["lines"] if l["result"] == "correct"][0]
    assert to_gtp(tuple(correct["moves"][0]), 9) == "B1", "角上直三急所应为 B1"
    assert WHITE != BLACK and other(BLACK) == WHITE


# ---------------------------------------------------------------- SGF 导入


def _problem_sgf(origin_title: str = "边上直三 黑先活") -> str:
    """用内置形状的摆子拼一份标准死活题 SGF（主线=正解，兄弟分支=失败图）。"""
    from app.game.rules import to_sgf
    shape = _shape("straight-three-edge")
    setup, _ = build_setup(shape)
    ab = "".join(f"[{to_sgf((x, y), 9)}]" for x, y, c in setup if c == BLACK)
    aw = "".join(f"[{to_sgf((x, y), 9)}]" for x, y, c in setup if c == WHITE)
    vital = to_sgf((2, 0), 9)
    bad = to_sgf((1, 0), 9)
    return (f"(;FF[4]GM[1]SZ[9]GN[{origin_title}]PL[B]AB{ab}AW{aw}"
            f"(;B[{vital}]C[正解：正中做两眼])"
            f"(;B[{bad}]C[失败：从端点入手];W[{vital}]C[白点后黑死]))")


def test_sgf_parser_keeps_variation_tree():
    """变化树不能被压平：主线与失败图要分得开，否则导入的就是错答案。"""
    from app.tsumego.sgfimport import parse_game_trees, unwrap_tree, _kids
    trees = parse_game_trees(_problem_sgf())
    assert len(trees) == 1
    root = unwrap_tree(trees[0])
    assert root["props"]["SZ"] == ["9"] and root["props"]["GN"] == ["边上直三 黑先活"]
    assert len(root["props"]["AB"]) == 7 and len(root["props"]["AW"]) == 7
    kids = _kids(root)
    assert len(kids) == 2, f"主线 + 失败图共两个分支，实际 {len(kids)}"
    assert kids[0]["props"]["B"] and kids[1]["props"]["B"]
    # 失败图自己还带一手白应（嵌套序列）
    assert len(_kids(kids[1])) == 1


def test_sgf_import_collection_and_rejects_non_problems():
    from app.tsumego.sgfimport import import_sgf_text
    # 一个文件里两道题（题集的常见形式）
    problems, skipped = import_sgf_text(_problem_sgf() + _problem_sgf("第二题 黑先活"),
                                        origin="collection.sgf")
    assert len(problems) == 2 and not skipped, skipped
    assert problems[0]["pid"] != problems[1]["pid"]
    p = problems[0]
    assert p["goal"] == GOAL_LIVE and p["toMove"] == BLACK and p["builtin"] is False
    assert p["space"], "边上直三的封闭局部必须推出搜索范围（L18：≤8 空点的外接矩形）"
    assert [l["result"] for l in p["lines"]] == ["correct", "wrong"]
    assert p["source"] == "collection.sgf"

    # 整盘棋谱（没有 AB/AW 摆子）不是死活题，必须被跳过而不是当成题目入库
    _, skipped2 = import_sgf_text("(;FF[4]SZ[9];B[cc];W[gg];B[cg])", origin="kifu.sgf")
    assert skipped2
    # 不受支持的棋盘尺寸
    _, skipped3 = import_sgf_text(_problem_sgf().replace("SZ[9]", "SZ[7]"), origin="odd.sgf")
    assert skipped3
    # 猜不出目标时不能乱入库（标题与注释里都不能出现「活/生/杀/死」类词）
    vague = (_problem_sgf("无名题目")
             .replace("正解：正中做两眼", "第一解")
             .replace("失败：从端点入手", "第二解")
             .replace("白点后黑死", "后续"))
    _, skipped4 = import_sgf_text(vague, origin="vague.sgf")
    assert skipped4, "既不含「活」也不含「杀」时必须要求显式指定 --goal"
    ok, _ = import_sgf_text(vague, origin="vague.sgf", goal=GOAL_KILL)
    assert ok and ok[0]["goal"] == GOAL_KILL


def test_imported_problem_is_playable_through_api(client, auth):
    """导入题全链路：入库 → 列表可见 → 正解判定 → 错着给失败图。"""
    from app.tsumego.sgfimport import import_sgf_text, save_problems
    headers, _ = auth
    problems, skipped = import_sgf_text(_problem_sgf(), origin="api-test.sgf",
                                        family="导入", source="测试用自制题")
    assert not skipped and len(problems) == 1
    assert save_problems(problems, replace=True) == 1
    pid = problems[0]["pid"]

    items = {i["id"]: i for i in
             client.get("/api/tsumego/problems", headers=headers).json()["items"]}
    assert pid in items and items[pid]["family"] == "导入"
    assert items[pid]["source"] == "测试用自制题", "出处与许可必须一路带到前端"
    assert "lines" not in items[pid]

    vital = [l for l in problems[0]["lines"] if l["result"] == "correct"][0]["moves"][0]
    body = client.post(f"/api/tsumego/{pid}/attempt", headers=headers,
                       json={"moves": [vital]}).json()
    assert body["status"] == "solved", body
    assert body["verdict"] in ("alive", "dead", "seki", "ko"), \
        f"导入题也应给死活结论（L18：局部搜索）：{body}"
    assert "两眼" in body["comment"]

    bad = [l for l in problems[0]["lines"] if l["result"] == "wrong"][0]["moves"][0]
    body2 = client.post(f"/api/tsumego/{pid}/attempt", headers=headers,
                        json={"moves": [bad]}).json()
    assert body2["status"] == "failed"
    assert body2["refutation"], "失败图要带上对方的应对"
    assert body2["solved"] is True, "已解出的标记不能被后来的失败抹除"
    assert body2["attempts"] == 2


def test_imported_problem_gets_local_search_for_offline_moves(client, auth):
    """L18：带局部搜索范围的导入题，线外落子由本地搜索给真实结论。

    旧口径：导入题 space=[]，落子不在 SGF 手顺线上就一句「不在正解变化里」。
    新口径：导入时推出外接矩形空间（≤8 空点）→ attempt 对线外落子跑局部搜索
    （默认真死活判据），给 verdict / 结论文案 / 对方最佳应对，文案里注明
    「导入题局部搜索结果」——可信度低于内置题，不许冒充内置结论。
    """
    from app.tsumego.sgfimport import import_sgf_text, save_problems
    headers, _ = auth
    problems, skipped = import_sgf_text(_problem_sgf(), origin="search.sgf")
    assert not skipped and len(problems) == 1
    assert problems[0]["space"], "边角直三的封闭局部必须推出搜索范围（L18）"
    assert save_problems(problems, replace=True) == 1
    pid = problems[0]["pid"]

    # 远离局部的线外落子：不匹配任何 SGF 线 → 走搜索兜底，结论必须带标记
    body = client.post(f"/api/tsumego/{pid}/attempt", headers=headers,
                       json={"moves": [[8, 0]]}).json()
    assert body["status"] in ("solved", "failed"), body
    assert "不在题目的正解变化里" not in body["comment"], body["comment"]
    assert body["verdict"], f"导入题线外落子没有给真实结论：{body}"
    assert "导入题局部搜索结果" in body["comment"], body["comment"]

    # 正解照旧走 SGF 手顺线
    vital = [l for l in problems[0]["lines"] if l["result"] == "correct"][0]["moves"][0]
    body2 = client.post(f"/api/tsumego/{pid}/attempt", headers=headers,
                        json={"moves": [vital]}).json()
    assert body2["status"] == "solved", body2

    # 摊开的导入图（外接框空点 >8）推不出空间 → 线外落子退回旧文案
    from app.game.rules import to_sgf
    wide = _problem_sgf("宽松 黑先活").replace("PL[B]AB", f"PL[B]AB[{to_sgf((8, 8), 9)}]", 1)
    p2, s2 = import_sgf_text(wide, origin="wide.sgf")
    assert p2 and not s2, s2
    assert p2[0]["space"] == [], "摊开的导入图不该推出搜索范围（>8 空点）"
    save_problems(p2, replace=True)
    body3 = client.post(f"/api/tsumego/{p2[0]['pid']}/attempt", headers=headers,
                        json={"moves": [[8, 0]]}).json()
    assert body3["status"] == "failed", body3
    assert body3["comment"] == "这一手不在题目的正解变化里。", body3["comment"]


def test_wrong_review_filter_returns_only_attempted_unsolved(client, auth):
    """错题重练：wrongOnly = 「做过且没解出」。

    钉：① 列表只含做错的题（没做过、已解出的都不出现）；
    ② /next 出题同口径；③ 做对后立刻从错题本消失，清空后 next 明确 404。
    """
    headers, _ = auth

    def _attempt(pid, move):
        return client.post(f"/api/tsumego/{pid}/attempt", headers=headers,
                           json={"moves": [move]}).json()

    # 一道题做错（用答案里的失败线第一手）
    wrong_problem = _fetch_problem(client, headers, kind=KIND_LIFE)
    ans = _answer_of(wrong_problem["id"])
    bad = [l for l in ans["lines"] if l["result"] == "wrong"]
    assert bad, "死活题应有失败线（fixture 前提）"
    assert _attempt(wrong_problem["id"], bad[0]["moves"][0])["status"] == "failed"

    # 另一道题做对
    right = _fetch_problem(client, headers, kind=KIND_KO)
    ans2 = _answer_of(right["id"])
    good = [l for l in ans2["lines"] if l["result"] == "correct"]
    assert good
    assert _attempt(right["id"], good[0]["moves"][0])["status"] == "solved"

    items = client.get("/api/tsumego/problems", headers=headers,
                       params={"wrongOnly": True}).json()["items"]
    ids = {i["id"] for i in items}
    assert wrong_problem["id"] in ids, "做错的题必须出现在错题本里"
    assert right["id"] not in ids, "已解出的题不进错题本"
    untouched = {i["id"] for i in client.get(
        "/api/tsumego/problems", headers=headers).json()["items"] if i["attempts"] == 0}
    assert not (ids & untouched), "没做过的题不该在错题本里"

    nxt = client.get("/api/tsumego/next", headers=headers,
                     params={"wrongOnly": True}).json()["problem"]
    assert nxt["id"] in ids, "出题口径要与列表一致"

    # 把错题做对 → 从错题本消失 → 清空后 next 404
    good_w = [l for l in ans["lines"] if l["result"] == "correct"][0]["moves"][0]
    assert _attempt(wrong_problem["id"], good_w)["status"] == "solved"
    assert client.get("/api/tsumego/problems", headers=headers,
                      params={"wrongOnly": True}).json()["items"] == []
    assert client.get("/api/tsumego/next", headers=headers,
                      params={"wrongOnly": True}).status_code == 404


def test_seed_builtin_is_not_overwritten_by_import(client, auth):
    """导入只写 builtin=False 的题，不能覆盖搜索推导出来的内置题。"""
    headers, _ = auth
    before = client.get("/api/tsumego/problems", headers=headers).json()["total"]
    builtin_before = sum(1 for i in
                         client.get("/api/tsumego/problems", headers=headers).json()["items"]
                         if i["builtin"])
    assert builtin_before >= 12
    from app.tsumego.store import seed_builtin
    seed_builtin()
    items = client.get("/api/tsumego/problems", headers=headers).json()["items"]
    assert len(items) == before
    assert sum(1 for i in items if i["builtin"]) == builtin_before
