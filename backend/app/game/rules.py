"""围棋规则引擎（纯 Python，不依赖 KataGo）。

负责：落子合法性（提子/自杀禁着/打劫与全局同形）、悔棋回滚、终局数子/数目、
让子摆放、坐标互转（GTP ⇄ 内部 ⇄ SGF）。

KataGo 只提供棋力与分析，合法性一律本地判定——这样即使弱档位也不会"作弊"，
并且在没有安装 KataGo 的机器上平台依然完整可用。
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

EMPTY, BLACK, WHITE = 0, 1, 2

# 打劫规则：POSITIONAL = 禁止全局同形（中国规则）；SITUATIONAL = 同形且同轮走子方（AGA）
KO_POSITIONAL = "POSITIONAL"
KO_SITUATIONAL = "SITUATIONAL"

# 计分方式
SCORE_AREA = "area"        # 数子（中国规则）
SCORE_TERRITORY = "territory"  # 数目（日韩规则）

Point = tuple[int, int]    # (x, y)，x 向右，y 向上（y=0 为 GTP 的第 1 行）

_GTP_LETTERS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"   # 跳过 I


class IllegalMove(Exception):
    """落子非法，message 直接可展示给玩家。"""


def other(color: int) -> int:
    return WHITE if color == BLACK else BLACK


def color_name(color: int) -> str:
    return "黑" if color == BLACK else "白"


# ---------------------------------------------------------------------------
# 坐标互转
# ---------------------------------------------------------------------------
def to_gtp(point: Point, size: int) -> str:
    x, y = point
    return f"{_GTP_LETTERS[x]}{y + 1}"


def from_gtp(text: str, size: int) -> Point:
    text = text.strip().upper()
    if text in ("PASS", ""):
        raise ValueError("pass")
    col, row = text[0], text[1:]
    x = _GTP_LETTERS.index(col)
    y = int(row) - 1
    if not (0 <= x < size and 0 <= y < size):
        raise ValueError(f"坐标越界: {text}")
    return (x, y)


def to_sgf(point: Point, size: int) -> str:
    x, y = point
    return chr(ord("a") + x) + chr(ord("a") + (size - 1 - y))


def from_sgf(text: str, size: int) -> Point:
    x = ord(text[0]) - ord("a")
    y = size - 1 - (ord(text[1]) - ord("a"))
    return (x, y)


def to_cn(point: Point, size: int) -> str:
    """中文讲解坐标：第 4 行 第 5 路 → 便于 LLM 与人阅读。"""
    x, y = point
    return f"({_GTP_LETTERS[x]}{y + 1})"


def handicap_stones(size: int, handicap: int) -> list[Point]:
    """标准让子摆放（星位）。"""
    if handicap < 2:
        return []
    if size % 2 == 0 or size < 9:
        raise ValueError("让子仅支持奇数路棋盘")
    c = size // 2
    # 星位：19 路与 13 路距边 3 线，9 路距边 2 线
    e = c - 3 if size >= 13 else c - 2
    lo, hi, mid = c - e, c + e, c
    corners = [(lo, lo), (hi, hi), (hi, lo), (lo, hi)]
    sides = [(lo, mid), (hi, mid), (mid, lo), (mid, hi)]
    order = {
        2: corners[:2], 3: corners[:3], 4: corners,
        5: corners + [(mid, mid)], 6: corners + [sides[0], sides[1]],
        7: corners + [sides[0], sides[1], (mid, mid)],
        8: corners + sides, 9: corners + sides + [(mid, mid)],
    }
    if handicap > 9:
        raise ValueError("最多让 9 子")
    return order[handicap]


# ---------------------------------------------------------------------------
# 单手棋
# ---------------------------------------------------------------------------
@dataclass
class Move:
    color: int
    point: Optional[Point]        # None = 虚手（pass）
    move_num: int                 # 从 1 开始
    captures: list[Point] = field(default_factory=list)

    @property
    def is_pass(self) -> bool:
        return self.point is None

    def to_dict(self, size: int) -> dict:
        return {
            "color": self.color,
            "x": None if self.point is None else self.point[0],
            "y": None if self.point is None else self.point[1],
            "sgf": "" if self.point is None else to_sgf(self.point, size),
            "gtp": "pass" if self.point is None else to_gtp(self.point, size),
            "moveNum": self.move_num,
            "captures": [[p[0], p[1]] for p in self.captures],
        }


# ---------------------------------------------------------------------------
# 棋盘
# ---------------------------------------------------------------------------
class Board:
    def __init__(self, size: int = 19, ko_rule: str = KO_POSITIONAL):
        if size not in (9, 13, 19):
            raise ValueError("仅支持 9/13/19 路棋盘")
        self.size = size
        self.ko_rule = ko_rule
        self.grid: list[list[int]] = [[EMPTY] * size for _ in range(size)]
        self.captured_by = {BLACK: 0, WHITE: 0}   # 各方提掉的子数（=对方死在盘上的子）
        self._position_hashes: set[str] = set()
        self._situational_hashes: set[str] = set()
        self.ko_point: Optional[Point] = None     # 简单劫争点（提示用，合法性由同形规则判定）
        self.move_count = 0
        self.pass_count = 0
        # 初始空盘也要进位置史：位置型 superko 判的是「这一手之后的局面此前是否出现过」，
        # 少了开局那一条，「回到空盘」的一手就永远是新的（理论上允许无限循环）。
        self._position_hashes.add(self._hash_position())

    # ---- 基础查询 ----
    def get(self, x: int, y: int) -> int:
        return self.grid[y][x]

    def at(self, point: Point) -> int:
        return self.grid[point[1]][point[0]]

    def inside(self, point: Point) -> bool:
        x, y = point
        return 0 <= x < self.size and 0 <= y < self.size

    def neighbors(self, point: Point) -> list[Point]:
        x, y = point
        out = []
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < self.size and 0 <= ny < self.size:
                out.append((nx, ny))
        return out

    def group(self, point: Point) -> tuple[list[Point], list[Point]]:
        """返回 (该点所属同色块的所有点, 该块的气)。"""
        color = self.at(point)
        if color == EMPTY:
            return [], []
        seen: set[Point] = {point}
        stack = [point]
        stones: list[Point] = []
        liberties: set[Point] = set()
        while stack:
            p = stack.pop()
            stones.append(p)
            for n in self.neighbors(p):
                v = self.at(n)
                if v == EMPTY:
                    liberties.add(n)
                elif v == color and n not in seen:
                    seen.add(n)
                    stack.append(n)
        return stones, sorted(liberties)

    def empty_points(self) -> list[Point]:
        return [(x, y) for y in range(self.size) for x in range(self.size)
                if self.grid[y][x] == EMPTY]

    def legal_moves(self, color: int) -> list[Point]:
        return [p for p in self.empty_points() if self.is_legal(color, p)]

    def is_legal(self, color: int, point: Point, explain: bool = False) -> bool:
        try:
            self._check(color, point, raise_error=True)
            return True
        except IllegalMove:
            if explain:
                raise
            return False

    # ---- 落子 ----
    def _hash_position(self) -> str:
        raw = bytes(v for row in self.grid for v in row)
        return hashlib.blake2b(raw, digest_size=16).hexdigest()

    def _check(self, color: int, point: Point, raise_error: bool = True) -> Optional[list[Point]]:
        """校验落子合法性，返回将被提掉的对方子。"""
        if not self.inside(point):
            raise IllegalMove("落点超出棋盘")
        if self.at(point) != EMPTY:
            raise IllegalMove("该点已有棋子")
        if self.ko_point is not None and point == self.ko_point:
            # 简单劫：先快速拦截，后面同形校验兜底（长生等复杂同形也能覆盖）
            raise IllegalMove("打劫禁着：需先在别处寻劫")

        x, y = point
        opp = other(color)
        # 试放，用于计算提子与自杀判定
        self.grid[y][x] = color
        # 用 dict 去重：新子的多个邻点可能属于同一个对方棋块
        captured_map: dict[Point, None] = {}
        for n in self.neighbors(point):
            if self.at(n) == opp and n not in captured_map:
                stones, libs = self.group(n)
                if not libs:
                    for s in stones:
                        captured_map[s] = None
        captured: list[Point] = list(captured_map)
        for cx, cy in captured:
            self.grid[cy][cx] = EMPTY
        _, my_libs = self.group(point)
        if not my_libs and not captured:
            self.grid[y][x] = EMPTY
            raise IllegalMove("禁止自杀手（该点无气且不能提子）")

        # 全局同形
        pos_hash = self._hash_position()
        sit_hash = pos_hash + str(color)
        forbidden = False
        if self.ko_rule == KO_POSITIONAL:
            forbidden = pos_hash in self._position_hashes
        else:
            forbidden = sit_hash in self._situational_hashes
        if forbidden:
            for cx, cy in captured:
                self.grid[cy][cx] = opp
            self.grid[y][x] = EMPTY
            raise IllegalMove("全局同形再现（禁着）")
        # 还原
        for cx, cy in captured:
            self.grid[cy][cx] = opp
        self.grid[y][x] = EMPTY
        return captured

    def play(self, color: int, point: Point) -> list[Point]:
        """正式落子，返回被提掉的点。非法时抛 IllegalMove。"""
        captured = self._check(color, point) or []
        x, y = point
        opp = other(color)
        self.grid[y][x] = color
        for cx, cy in captured:
            self.grid[cy][cx] = EMPTY
        self.captured_by[color] += len(captured)

        # 劫点判定：单子、单气、且恰好提掉一子
        self.ko_point = None
        if len(captured) == 1:
            stones, libs = self.group(point)
            if len(stones) == 1 and len(libs) == 1:
                self.ko_point = captured[0]

        self._position_hashes.add(self._hash_position())
        self._situational_hashes.add(self._hash_position() + str(color))
        self.move_count += 1
        self.pass_count = 0
        return captured

    def play_pass(self, color: int) -> None:
        self.move_count += 1
        self.pass_count += 1
        self.ko_point = None

    # ---- 快照（悔棋时整盘重建用）----
    def snapshot(self) -> dict:
        return {
            "grid": [row[:] for row in self.grid],
            "captured_by": dict(self.captured_by),
            "pos_hashes": set(self._position_hashes),
            "sit_hashes": set(self._situational_hashes),
            "ko_point": self.ko_point,
            "move_count": self.move_count,
            "pass_count": self.pass_count,
        }

    def restore(self, snap: dict) -> None:
        self.grid = [row[:] for row in snap["grid"]]
        self.captured_by = dict(snap["captured_by"])
        self._position_hashes = set(snap["pos_hashes"])
        self._situational_hashes = set(snap["sit_hashes"])
        self.ko_point = snap["ko_point"]
        self.move_count = snap["move_count"]
        self.pass_count = snap["pass_count"]

    # ---- 计分 ----
    def territory_regions(self, dead: Iterable[Point] = ()) -> dict[str, list[Point]]:
        """把空点按"被单一颜色包围"划分区域，返回 black/white/neutral 三类空点。"""
        dead_set = set(dead)
        eff = [row[:] for row in self.grid]
        for (x, y) in dead_set:
            eff[y][x] = EMPTY

        seen = [[False] * self.size for _ in range(self.size)]
        black_area, white_area, neutral = [], [], []
        for y in range(self.size):
            for x in range(self.size):
                if eff[y][x] != EMPTY or seen[y][x]:
                    continue
                region, borders = self._flood_empty(eff, (x, y), seen)
                if borders == {BLACK}:
                    black_area.extend(region)
                elif borders == {WHITE}:
                    white_area.extend(region)
                else:
                    neutral.extend(region)
        return {"black": black_area, "white": white_area, "neutral": neutral}

    def _flood_empty(self, eff: list[list[int]], start: Point,
                     seen: list[list[bool]]) -> tuple[list[Point], set[int]]:
        stack = [start]
        seen[start[1]][start[0]] = True
        region: list[Point] = []
        borders: set[int] = set()
        while stack:
            x, y = stack.pop()
            region.append((x, y))
            for nx, ny in self.neighbors((x, y)):
                v = eff[ny][nx]
                if v == EMPTY:
                    if not seen[ny][nx]:
                        seen[ny][nx] = True
                        stack.append((nx, ny))
                else:
                    borders.add(v)
        return region, borders

    def score(self, komi: float, method: str = SCORE_AREA,
              dead: Iterable[Point] = ()) -> dict:
        """终局计分。dead = 被判定为死子的点（含单官里的死子）。"""
        dead_set = set(dead)
        regions = self.territory_regions(dead_set)
        black_stones = sum(1 for y in range(self.size) for x in range(self.size)
                           if self.grid[y][x] == BLACK and (x, y) not in dead_set)
        white_stones = sum(1 for y in range(self.size) for x in range(self.size)
                           if self.grid[y][x] == WHITE and (x, y) not in dead_set)

        black_territory = len(regions["black"])
        white_territory = len(regions["white"])

        if method == SCORE_AREA:
            # 数子（中国规则）：活子 + 围住的空点。
            # 死子已被移除，其所在点会自然归入包围方的地域，不重复计数。
            black_total = black_stones + black_territory
            white_total = white_stones + white_territory + komi
        else:
            # 数目（日韩规则）：地域 + 提子（盘上死子计为俘虏）
            black_total = black_territory + self.captured_by[BLACK] + \
                sum(1 for p in dead_set if self.grid[p[1]][p[0]] == WHITE)
            white_total = white_territory + self.captured_by[WHITE] + komi + \
                sum(1 for p in dead_set if self.grid[p[1]][p[0]] == BLACK)

        diff = black_total - white_total
        if abs(diff) < 1e-9:
            winner, result_text = EMPTY, "和棋（jigo）"
        elif diff > 0:
            winner, result_text = BLACK, f"黑胜 {diff:g} 目/子"
        else:
            winner, result_text = WHITE, f"白胜 {-diff:g} 目/子"
        return {
            "method": method,
            "blackStones": black_stones,
            "whiteStones": white_stones,
            "blackTerritory": black_territory,
            "whiteTerritory": white_territory,
            "komi": komi,
            "blackTotal": round(black_total, 1),
            "whiteTotal": round(white_total, 1),
            "diff": round(diff, 1),
            "winner": winner,
            "result": result_text,
            "deadStones": [[p[0], p[1]] for p in sorted(dead_set)],
        }

    def is_over(self) -> bool:
        return self.pass_count >= 2

    # ---- 调试 ----
    def to_ascii(self) -> str:
        chars = {EMPTY: ".", BLACK: "X", WHITE: "O"}
        lines = ["   " + " ".join(_GTP_LETTERS[:self.size])]
        for y in range(self.size - 1, -1, -1):
            lines.append(f"{y + 1:>2} " + " ".join(chars[self.grid[y][x]] for x in range(self.size)))
        return "\n".join(lines)

    def place(self, color: int, points: Sequence[Point]) -> None:
        """直接摆子（让子/复盘导入用），不做合法性校验。"""
        for (x, y) in points:
            self.grid[y][x] = color
        self._position_hashes.add(self._hash_position())


# ---------------------------------------------------------------------------
# 完整对局
# ---------------------------------------------------------------------------
class Game:
    """一局棋：棋盘 + 手顺 + 悔棋 + 终局。"""

    def __init__(self, size: int = 19, komi: float = 7.5, handicap: int = 0,
                 ko_rule: str = KO_POSITIONAL, score_method: str = SCORE_AREA,
                 player_color: int = BLACK, seed: Optional[int] = None):
        self.size = size
        self.komi = komi
        self.handicap = handicap
        self.ko_rule = ko_rule
        self.score_method = score_method
        self.player_color = player_color
        self.board = Board(size, ko_rule)
        self.moves: list[Move] = []
        self.result: Optional[dict] = None
        self.finished = False
        self.finish_reason: Optional[str] = None
        self.rng = random.Random(seed)

        if handicap >= 2:
            stones = handicap_stones(size, handicap)
            self.board.place(BLACK, stones)
            self.next_color = WHITE
            self.handicap_stones = stones
        else:
            self.next_color = BLACK
            self.handicap_stones = []

    # ---- 走子 ----
    def play(self, color: int, point: Optional[Point]) -> Move:
        if self.finished:
            raise IllegalMove("对局已结束")
        if color != self.next_color:
            raise IllegalMove("还没轮到你落子")
        if point is None:
            self.board.play_pass(color)
            move = Move(color=color, point=None, move_num=len(self.moves) + 1)
        else:
            captured = self.board.play(color, point)
            move = Move(color=color, point=point, move_num=len(self.moves) + 1, captures=captured)
        self.moves.append(move)
        self.next_color = other(color)
        if self.board.is_over():
            self.finish("pass-pass")
        return move

    def ai_color(self) -> int:
        return other(self.player_color)

    def finish(self, reason: str, winner: Optional[int] = None,
               result_text: Optional[str] = None, dead: Iterable[Point] = ()) -> dict:
        self.finished = True
        self.finish_reason = reason
        if reason == "pass-pass":
            self.result = self.board.score(self.komi, self.score_method, dead)
        elif winner == 0:
            # 无胜者（强制结束 / 作废对局）。不能落到下面的兜底推断：那里会从
            # result_text 里猜一个颜色出来（没有"黑"字就判白胜），于是 result_json
            # 凭空多出一个胜方，历史记录与日历胜率统计都会被这条假胜负污染。
            self.result = {
                "method": self.score_method, "winner": 0,
                "result": result_text or "无结果",
                "diff": None, "blackTotal": None, "whiteTotal": None,
                "blackStones": None, "whiteStones": None,
                "blackTerritory": None, "whiteTerritory": None,
                "komi": self.komi, "deadStones": [],
            }
        else:
            # 认输 / 超时 / AI 投子
            w = winner if winner in (BLACK, WHITE) else (
                BLACK if (result_text and "黑" in result_text) else WHITE)
            self.result = {
                "method": self.score_method, "winner": w,
                "result": result_text or ("黑胜（对方认输）" if w == BLACK else "白胜（对方认输）"),
                "diff": None, "blackTotal": None, "whiteTotal": None,
                "blackStones": None, "whiteStones": None,
                "blackTerritory": None, "whiteTerritory": None,
                "komi": self.komi, "deadStones": [],
            }
        return self.result

    # ---- 悔棋 ----
    def takeback(self, plies: int = 2) -> list[Move]:
        """回滚 plies 手（默认 2 = 玩家 + AI 各一手）。返回被撤销的手。"""
        if not self.moves:
            return []
        plies = max(1, min(plies, len(self.moves)))
        undone: list[Move] = []
        for _ in range(plies):
            if not self.moves:
                break
            undone.append(self.moves.pop())
        self.board = self._rebuild_board()
        self.finished = False
        self.finish_reason = None
        self.result = None
        self.next_color = other(self.moves[-1].color) if self.moves else (
            WHITE if self.handicap >= 2 else BLACK)
        return undone[::-1]

    def _rebuild_board(self) -> Board:
        b = Board(self.size, self.ko_rule)
        if self.handicap_stones:
            b.place(BLACK, self.handicap_stones)
        for m in self.moves:
            if m.point is None:
                b.play_pass(m.color)
            else:
                b.play(m.color, m.point)
        return b

    # ---- 快照 ----
    def to_dict(self, include_board: bool = True) -> dict:
        d = {
            "size": self.size, "komi": self.komi, "handicap": self.handicap,
            "playerColor": self.player_color, "nextColor": self.next_color,
            "koRule": self.ko_rule, "scoreMethod": self.score_method,
            "finished": self.finished, "finishReason": self.finish_reason,
            "result": self.result,
            "moveCount": len(self.moves),
            "handicapStones": [[p[0], p[1]] for p in self.handicap_stones],
            "koPoint": list(self.board.ko_point) if self.board.ko_point else None,
            "captures": {"black": self.board.captured_by[BLACK], "white": self.board.captured_by[WHITE]},
            "moves": [m.to_dict(self.size) for m in self.moves],
        }
        if include_board:
            d["board"] = [row[:] for row in self.board.grid]
        return d

    def gtp_moves(self) -> list[dict]:
        """KataGo analysis 查询用的 moves 数组。"""
        out = []
        for m in self.moves:
            out.append({
                "player": "B" if m.color == BLACK else "W",
                "move": "pass" if m.point is None else to_gtp(m.point, self.size),
            })
        return out

    def initial_stones_gtp(self) -> list[list]:
        if not self.handicap_stones:
            return []
        return [["B", to_gtp(p, self.size)] for p in self.handicap_stones]
