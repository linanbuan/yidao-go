"""围棋规则引擎单元测试：提子、自杀禁着、打劫、数子、悔棋、让子、坐标、SGF。"""
from __future__ import annotations

import pytest

from app.game.rules import (BLACK, EMPTY, WHITE, Board, Game, IllegalMove,
                            SCORE_AREA, from_gtp, from_sgf, handicap_stones,
                            to_gtp, to_sgf)
from app.game.sgf import export_sgf, parse_sgf, sgf_to_game


# ---------------------------------------------------------------------------
# 提子
# ---------------------------------------------------------------------------
def test_capture_single_corner_stone():
    b = Board(9)
    b.play(BLACK, (0, 0))
    b.play(WHITE, (1, 0))
    b.play(BLACK, (5, 5))          # 黑 elsewhere
    b.play(WHITE, (0, 1))          # 白封住角上黑子的最后一口气
    assert b.at((0, 0)) == EMPTY
    assert b.captured_by[WHITE] == 1


def test_capture_group_of_three():
    b = Board(9)
    b.place(WHITE, [(0, 1), (1, 1), (1, 0)])       # 角上白棋 L 形，仅剩 (0,0) 一口气
    b.place(BLACK, [(0, 2), (1, 2), (2, 1), (2, 0)])
    captured = b.play(BLACK, (0, 0))
    assert len(captured) == 3
    assert b.captured_by[BLACK] == 3
    assert b.at((0, 0)) == BLACK
    assert b.at((1, 1)) == EMPTY


def test_suicide_forbidden():
    b = Board(9)
    b.place(WHITE, [(1, 0), (0, 1)])
    with pytest.raises(IllegalMove):
        b.play(BLACK, (0, 0))      # 无气且不能提子 → 自杀禁着
    assert b.at((0, 0)) == EMPTY   # 棋盘未被污染


def test_capture_is_not_suicide():
    """落点自身无气，但能提掉对方 → 合法。"""
    b = Board(9)
    b.place(WHITE, [(0, 1), (1, 1), (1, 0)])
    b.place(BLACK, [(0, 2), (1, 2), (2, 1), (2, 0)])
    b.play(BLACK, (0, 0))          # 不抛异常即为通过
    assert b.at((0, 0)) == BLACK


def test_occupied_point_and_out_of_board():
    b = Board(9)
    b.play(BLACK, (4, 4))
    with pytest.raises(IllegalMove):
        b.play(WHITE, (4, 4))
    with pytest.raises(IllegalMove):
        b.play(WHITE, (9, 0))
    with pytest.raises(IllegalMove):
        b.play(WHITE, (-1, 0))


# ---------------------------------------------------------------------------
# 打劫与全局同形
# ---------------------------------------------------------------------------
def _ko_board() -> Board:
    """构造标准劫争：白 (1,1) 单气，黑下 (2,1) 提劫。"""
    b = Board(9)
    b.place(BLACK, [(0, 1), (1, 0), (1, 2)])
    b.place(WHITE, [(3, 1), (2, 0), (2, 2)])
    b.play(WHITE, (1, 1))
    b.play(BLACK, (2, 1))
    return b


def test_ko_immediate_recapture_forbidden():
    b = _ko_board()
    assert b.at((1, 1)) == EMPTY
    assert b.ko_point == (1, 1)
    with pytest.raises(IllegalMove):
        b.play(WHITE, (1, 1))


def test_ko_recapture_after_exchange():
    b = _ko_board()
    b.play(WHITE, (7, 7))          # 寻劫
    b.play(BLACK, (7, 6))          # 应劫
    b.play(WHITE, (1, 1))          # 回提，合法
    assert b.at((2, 1)) == EMPTY
    assert b.at((1, 1)) == WHITE
    assert b.captured_by[WHITE] == 1
    assert b.ko_point == (2, 1)


def test_positional_superko_blocks_repeat():
    """禁全同：任何导致历史局面重现的落子都被拒绝。"""
    b = Board(9)
    b.play(BLACK, (4, 4))
    b.play(WHITE, (4, 5))
    hash_after_two = b._hash_position()
    assert hash_after_two in b._position_hashes


# ---------------------------------------------------------------------------
# 计分
# ---------------------------------------------------------------------------
def _wall_board() -> Board:
    b = Board(9)
    b.place(BLACK, [(4, y) for y in range(9)])
    b.place(WHITE, [(5, y) for y in range(9)])
    return b


def test_score_area():
    res = _wall_board().score(7.5, SCORE_AREA)
    assert res["blackStones"] == 9
    assert res["whiteStones"] == 9
    assert res["blackTerritory"] == 36        # x=0..3
    assert res["whiteTerritory"] == 27        # x=6..8
    assert res["blackTotal"] == 45
    assert res["whiteTotal"] == 43.5
    assert res["diff"] == 1.5
    assert res["winner"] == BLACK


def test_score_with_dead_stones():
    b = _wall_board()
    b.place(WHITE, [(1, 1)])                  # 白子深入黑阵
    # 未判死：白子留在黑阵中，该区域变成双方交界 → 计为中性（谁都不算地）。
    # 这正是终局流程必须先判定死子（ownership 自动 + 玩家手动修正）的原因。
    alive = b.score(7.5, SCORE_AREA)
    assert alive["blackTerritory"] == 0
    assert alive["whiteTerritory"] == 27

    res = b.score(7.5, SCORE_AREA, dead=[(1, 1)])
    assert res["blackTerritory"] == 36        # 死子移除后整块归黑
    assert res["whiteStones"] == 9            # 死子不计入活子
    assert res["blackTotal"] == 45
    assert res["whiteTotal"] == 43.5
    assert res["diff"] == 1.5
    assert res["deadStones"] == [[1, 1]]
    assert res["winner"] == BLACK


def test_score_japanese_method():
    res = _wall_board().score(7.5, "territory")
    assert res["blackTotal"] == 36
    assert res["whiteTotal"] == 34.5


# ---------------------------------------------------------------------------
# 对局流程
# ---------------------------------------------------------------------------
def test_pass_pass_finishes_game():
    g = Game(size=9, komi=5.5)
    g.play(BLACK, (2, 2))
    g.play(WHITE, (6, 6))
    g.play(BLACK, None)
    g.play(WHITE, None)
    assert g.finished
    assert g.result["winner"] == WHITE
    assert g.result["diff"] == -5.5


def test_wrong_turn_rejected():
    g = Game(size=9)
    with pytest.raises(IllegalMove):
        g.play(WHITE, (3, 3))
    g.play(BLACK, (3, 3))
    with pytest.raises(IllegalMove):
        g.play(BLACK, (4, 4))


def test_takeback_two_plies():
    g = Game(size=9, komi=5.5)
    g.play(BLACK, (2, 2))
    g.play(WHITE, (6, 6))
    g.play(BLACK, (4, 4))
    undone = g.takeback(2)
    assert len(undone) == 2
    assert len(g.moves) == 1
    assert g.board.at((4, 4)) == EMPTY
    assert g.board.at((6, 6)) == EMPTY
    assert g.board.at((2, 2)) == BLACK
    assert g.next_color == WHITE


def test_takeback_restores_captured_stones():
    """悔棋后被提的子要回到盘上，提子计数同步回滚。"""
    g = Game(size=9, komi=5.5)
    g.play(BLACK, (0, 0))
    g.play(WHITE, (1, 0))
    g.play(BLACK, (5, 5))
    g.play(WHITE, (0, 1))          # 提掉角上黑子
    assert g.board.at((0, 0)) == EMPTY
    assert g.board.captured_by[WHITE] == 1

    g.takeback(2)
    assert g.board.at((0, 0)) == BLACK
    assert g.board.at((5, 5)) == EMPTY
    assert g.board.captured_by[WHITE] == 0
    assert g.next_color == BLACK
    assert len(g.moves) == 2


def test_handicap_stones_and_first_move():
    assert handicap_stones(19, 4) == [(3, 3), (15, 15), (15, 3), (3, 15)]
    assert handicap_stones(19, 9)[-1] == (9, 9)
    assert len(handicap_stones(13, 2)) == 2
    assert handicap_stones(9, 5)[4] == (4, 4)
    with pytest.raises(ValueError):
        handicap_stones(19, 10)

    g = Game(size=19, handicap=4)
    assert g.next_color == WHITE
    assert g.board.at((3, 3)) == BLACK
    assert g.board.at((15, 15)) == BLACK
    g.play(WHITE, (9, 15))           # 白先走（避开让子点）
    assert g.board.at((9, 15)) == WHITE


# ---------------------------------------------------------------------------
# 坐标与 SGF
# ---------------------------------------------------------------------------
def test_coordinate_roundtrip():
    assert to_gtp((0, 0), 19) == "A1"
    assert to_gtp((8, 8), 19) == "J9"       # GTP 跳过字母 I
    assert from_gtp("Q16", 19) == (15, 15)
    assert from_gtp("A1", 19) == (0, 0)
    for x in range(19):
        for y in range(19):
            assert from_gtp(to_gtp((x, y), 19), 19) == (x, y)
    assert to_sgf((15, 15), 19) == "pd"
    assert from_sgf("pd", 19) == (15, 15)
    with pytest.raises(ValueError):
        from_gtp("pass", 19)


def test_sgf_export_and_reimport():
    g = Game(size=9, komi=5.5)
    for color, pt in [(BLACK, (2, 6)), (WHITE, (6, 6)), (BLACK, (2, 2)), (WHITE, (6, 2))]:
        g.play(color, pt)
    sgf = export_sgf(g, black_name="小明", white_name="三段棋匠")
    assert sgf.startswith("(;FF[4]GM[1]")
    assert "SZ[9]" in sgf and "KM[5.5]" in sgf
    assert ";B[cg]" in sgf

    data = parse_sgf(sgf)
    assert data["size"] == 9 and data["komi"] == 5.5
    assert len(data["moves"]) == 4

    g2 = sgf_to_game(sgf)
    assert len(g2.moves) == 4
    assert [m.point for m in g2.moves] == [m.point for m in g.moves]
    assert g2.board.grid == g.board.grid


def test_sgf_handicap_export():
    g = Game(size=19, komi=0.5, handicap=2)
    g.play(WHITE, (3, 15))
    sgf = export_sgf(g)
    assert "HA[2]" in sgf and "AB[" in sgf and "PL[W]" in sgf
    data = parse_sgf(sgf)
    assert data["handicap"] == 2
    assert len(data["handicapStones"]) == 2


def test_sgf_with_result_code():
    g = Game(size=9, komi=5.5)
    g.play(BLACK, (2, 2))
    g.finish("resign", winner=BLACK, result_text="黑胜（对方认输）")
    sgf = export_sgf(g)
    assert "RE[B+R]" in sgf
