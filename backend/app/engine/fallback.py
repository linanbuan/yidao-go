"""内置启发式引擎（KataGo 缺席时的降级方案）。

输出与 KataGo analysis 相同结构的结果（候选点 + 胜率 + 目差 + ownership 热力），
因此上层代码无需分支判断，前端体验保持一致，只是棋力与精度有限。

用途：
  1. 开发/演示环境没有 KataGo 时，平台全流程（对局、胜率曲线、复盘）依然可用；
  2. 极低档位（18级附近）的对手本来就不需要强搜索，用启发式反而更"像初学者"。

估值口径（重要，两处设计都是为了「损失目数」与胜率曲线可信）：
  静态影响力估值天然偏爱子多的一方（多一子就多几分控制区），
  所以 base_eval 不能直接当作局面价值：相邻两手总差着一子，
  跳节点目差会恒定得出"每手都赚 ~5 目"的假结果。

  1. 先用双方对称的试下估出「一手棋的先手价值」tempo：
         tempo(P) = ( best_black(P) - best_white(P) ) / 2
     再按行棋方扣掉多出来的那一子：
         root(P) = base_eval(P) - (0 if 黑先走 else 1) × tempo
     黑先走时双方子数相同不用扣；白先走时黑多一子，扣一个 tempo。
     （实测：不扣时黑视角 root 逐手锯齿 ≈ 5.4 目，扣后降到 ≈ 0.3 目，
       且 24 手平均对局后的 root 均值仍回到空盘的 -komi）
  2. 候选点的 scoreLead 统一换算到「落子后那个局面的 root 口径」：
         黑先走 → 落子后 extra 从 0 变 1，要减一个 tempo；
         白先走 → 落子后 extra 从 1 变 0，本身已是 root 口径，不用修。
     这样「最佳候选 ≈ rootInfo」，与 KataGo 的 moveInfos/rootInfo 语义对齐；
     同一节点内的候选差值就是这一手的损失目数（见 review.analyzer）。
"""
from __future__ import annotations

import math
import random
from typing import Optional

from ..game.rules import (BLACK, EMPTY, WHITE, Board, Point, from_gtp, other,
                          to_gtp)
from .protocol import AnalysisQuery, AnalysisResult, Candidate

FIELD_RADIUS = 4          # 影响力核半径（距离 4 以外权重 < 0.06，可忽略）
OWNERSHIP_GAIN = 8.0      # 热力图放大系数（tanh 饱和用）
SHARE_WEIGHT = 0.32       # 估值用：空点归属份额的线性权重
EVAL_SCALE = 30.0         # 目差 → 胜率 的缩放
BASELINE_TAU = 4.0        # root 估值的软化温度（目）：越小越接近 max/min
TOP_K = 8                 # 参与 root 估值的基准点数
MAX_CANDIDATES = 12       # 对外报告的候选点上限


def _soft_extreme(values: list[float], tau: float, maximize: bool = True) -> float:
    """软最大/软最小：比硬 max/min 稳健（不会被单个启发式离群值带偏）。"""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    sign = 1.0 if maximize else -1.0
    top = max(sign * v for v in values)
    wsum = acc = 0.0
    for v in values:
        w = math.exp((sign * v - top) / max(0.01, tau))
        wsum += w
        acc += w * v
    return acc / wsum if wsum else values[0]


class HeuristicEngine:
    name = "heuristic"

    def __init__(self, seed: Optional[int] = None):
        self.rng = random.Random(seed)

    # ------------------------------------------------------------------
    def analyze(self, q: AnalysisQuery, side_to_move: int, turn: int = 0,
                noise: float = 0.3) -> AnalysisResult:
        board = self._rebuild(q, len(q.moves))
        return self._evaluate(board, q, side_to_move, turn, noise)

    def analyze_turns(self, q: AnalysisQuery, turns: list[int],
                      side_to_move_seq: list[int], noise: float = 0.0) -> list[AnalysisResult]:
        out: list[AnalysisResult] = []
        board: Optional[Board] = None
        prev = 0
        for i, t in enumerate(turns):
            if board is None or t < prev:
                board = self._rebuild(q, t)
            else:
                # turns 递增时增量推进：从上一手局面只补几手，而不是每手都
                # 从头重建整盘（重建是 O(手数²)，200 手的棋谱会慢到分钟级）
                for item in q.moves[prev:t]:
                    self._play_move(board, item)
            stm = side_to_move_seq[i] if i < len(side_to_move_seq) else BLACK
            # 复盘时把「当时实际下出的一手」强制纳入候选，
            # 否则它跌出前 12 名时就算不出同节点损失（见 review.analyzer.node_loss）
            out.append(self._evaluate(board, q, stm, t, noise,
                                      force_point=self._actual_move(q, t)))
            prev = t
        return out

    @staticmethod
    def _actual_move(q: AnalysisQuery, turn: int) -> Optional[Point]:
        if turn >= len(q.moves):
            return None
        mv = str(q.moves[turn].get("move", ""))
        if not mv or mv.lower() == "pass":
            return None
        try:
            return from_gtp(mv, q.size)
        except ValueError:
            return None

    @staticmethod
    def _play_move(board: Board, item) -> None:
        """把查询里的一手应用到棋盘（非法手容错跳过）。"""
        c = BLACK if str(item["player"]).upper().startswith("B") else WHITE
        mv = str(item["move"])
        if mv.lower() == "pass":
            board.play_pass(c)
            return
        p = from_gtp(mv, board.size)
        if board.at(p) == EMPTY:
            try:
                board.play(c, p)
            except Exception:   # noqa: BLE001  非法手直接跳过（导入棋谱容错）
                pass

    # ------------------------------------------------------------------
    def _rebuild(self, q: AnalysisQuery, upto: int) -> Board:
        """按查询里的 initialStones + moves 重建指定手数的局面。"""
        board = Board(q.size)
        for item in q.initial_stones or []:
            color = BLACK if str(item[0]).upper().startswith("B") else WHITE
            board.place(color, [from_gtp(str(item[1]), q.size)])
        for item in q.moves[:upto]:
            self._play_move(board, item)
        return board

    # ------------------------------------------------------------------
    def _evaluate(self, board: Board, q: AnalysisQuery, side_to_move: int,
                  turn: int, noise: float,
                  force_point: Optional[Point] = None) -> AnalysisResult:
        size = board.size
        move_num = board.move_count
        field = self._influence_field(board)
        base_eval = self._eval_from_field(board, field, q.komi)

        legal = board.legal_moves(side_to_move)
        clean: list[tuple[float, Point]] = []
        for p in legal:
            if self._is_own_eye(board, p, side_to_move):
                continue
            clean.append((self._score_move(board, side_to_move, p, move_num, size), p))
        clean.sort(key=lambda t: -t[0])

        # 噪声只影响「展示/选点」的次序，不污染 root 估值（否则胜率曲线会随机抖）
        if noise:
            display = sorted(((s + self.rng.gauss(0, noise * 6), p) for s, p in clean),
                             key=lambda t: -t[0])
        else:
            display = clean
        shown: list[tuple[float, Optional[Point]]] = list(display[:MAX_CANDIDATES])
        # 强制纳入指定点（复盘时实际下出的一手）：挡不进去就挤掉最后一名
        if force_point is not None and board.at(force_point) == EMPTY \
                and all(p != force_point for _, p in shown):
            fs = self._score_move(board, side_to_move, force_point, move_num, size)
            if len(shown) >= MAX_CANDIDATES:
                shown[-1] = (fs, force_point)
            else:
                shown.append((fs, force_point))
        # 收官末期允许虚手
        if not shown or move_num > size * size * 0.85:
            shown.append((-999.0, None))

        # 试下估值缓存：root 基准与候选点共用，同一 (颜色, 点) 只算一次
        cache: dict[tuple[int, Point], Optional[float]] = {}

        def trial(color: int, point: Point) -> Optional[float]:
            key = (color, point)
            if key not in cache:
                cache[key] = self._eval_after_move(board, field, q.komi, color, point)
            return cache[key]

        # ---- 先手价值 tempo：同一批点上黑/白各先下一手，取软最大与软最小 ----
        black_vals: list[float] = []
        white_vals: list[float] = []
        for _, p in clean[:TOP_K]:
            vb = trial(BLACK, p)
            vw = trial(WHITE, p)
            if vb is not None:
                black_vals.append(vb)
            if vw is not None:
                white_vals.append(vw)
        if black_vals and white_vals:
            best_black = _soft_extreme(black_vals, BASELINE_TAU, maximize=True)
            best_white = _soft_extreme(white_vals, BASELINE_TAU, maximize=False)
            tempo = (best_black - best_white) / 2.0   # 一手棋的先手价值（黑视角）
        else:
            tempo = 0.0
        # 白先走 ⇒ 黑刚多下了一子，把这一子的先手价值从形势里扣掉
        root_eval = base_eval - (0.0 if side_to_move == BLACK else tempo)
        # 候选值换算到落子后局面的 root 口径：黑落子后多一子要扣 tempo，
        # 白落子后双方子数持平，已经是 root 口径
        adj = tempo if side_to_move == BLACK else 0.0

        total_visits = max(8, q.max_visits)
        best_score = shown[0][0]
        candidates: list[Candidate] = []
        for rank, (s, p) in enumerate(shown):
            w = math.exp(max(-6.0, (s - best_score) / 2.0))
            if p is None:
                score = root_eval           # 虚手：启发式无法估先手损失，当作局面不变
            else:
                v = trial(side_to_move, p)
                score = (base_eval if v is None else v) - adj
            mover_view = score if side_to_move == BLACK else -score
            candidates.append(Candidate(
                point=p, gtp="pass" if p is None else to_gtp(p, size),
                visits=max(1, int(round(w * total_visits))),
                winrate=self._clamp01(0.5 + math.tanh((mover_view + q.komi) / EVAL_SCALE) * 0.45),
                score_lead=round(score, 2),
                rank=rank,
            ))

        winrate_black = self._clamp01(0.5 + math.tanh((root_eval + q.komi) / EVAL_SCALE) * 0.45)
        winrate_stm = winrate_black if side_to_move == BLACK else 1.0 - winrate_black

        return AnalysisResult(
            turn=turn,
            side_to_move=side_to_move,
            candidates=candidates,
            winrate=winrate_stm,
            score_lead=round(root_eval, 2),
            visits=sum(c.visits for c in candidates),
            ownership=self._ownership_from_field(board, field),   # 内部口径，见该方法注释
            notes="内置启发式引擎（未检测到 KataGo，棋力与精度有限）",
            engine=self.name,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _clamp01(v: float) -> float:
        return max(0.0, min(1.0, v))

    def _is_own_eye(self, board: Board, point: Point, color: int) -> bool:
        nbrs = board.neighbors(point)
        if not nbrs or any(board.at(n) != color for n in nbrs):
            return False
        x, y = point
        diag_ok, diag_total = 0, 0
        for dx, dy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < board.size and 0 <= ny < board.size:
                diag_total += 1
                if board.grid[ny][nx] == color:
                    diag_ok += 1
        return diag_total == 0 or diag_ok >= max(1, diag_total - 1)

    def _score_move(self, board: Board, color: int, point: Point,
                    move_num: int, size: int) -> float:
        """候选点排序启发式（棋形/攻防/线位/间距），与形势估值分工不同。"""
        opp = other(color)
        score = 0.0
        x, y = point

        # 提子 / 救棋 / 叫吃
        board.grid[y][x] = color
        capture_count = 0
        atari_saved = False
        for n in board.neighbors(point):
            if board.at(n) == opp:
                stones, libs = board.group(n)
                if not libs:
                    capture_count += len(stones)
                elif len(libs) == 1:
                    score += 10.0
            elif board.at(n) == color:
                stones, libs = board.group(n)
                if len(libs) == 1:
                    atari_saved = True
        board.grid[y][x] = EMPTY
        score += capture_count * 25.0
        if atari_saved:
            score += 18.0

        # 自身安全性：落子后不能只剩一口气（送吃）
        stones_after, libs_after = self._group_if_played(board, color, point)
        if len(libs_after) <= 1 and capture_count == 0:
            score -= 22.0
        elif len(libs_after) == 2:
            score += 3.0
        if len(stones_after) == 1 and move_num > size:
            score -= 2.0     # 中盘孤子

        # 线位偏好：布局阶段偏爱三、四线与星位
        line = min(x, y, size - 1 - x, size - 1 - y) + 1
        if move_num < size * 2:
            score += {1: -25.0, 2: -8.0, 3: 10.0, 4: 12.0, 5: 4.0}.get(line, 0.0)
            c = (size - 1) / 2
            dist_center = math.hypot(x - c, y - c)
            score += max(0.0, 6.0 - dist_center * 0.6)
        else:
            score += {1: -6.0, 2: 2.0, 3: 4.0}.get(line, 1.0)

        # 接触与间距：贴近已有棋子（但不贴死）
        near = self._nearest_stone_dist(board, point)
        if near is not None:
            score += {1: -4.0, 2: 8.0, 3: 6.0, 4: 2.0}.get(near, -1.0)
            if near >= 7 and move_num < size * 2:
                score -= 5.0   # 布局阶段过于脱先

        # 官子阶段偏向能围住实地的点
        if move_num > size * size * 0.6:
            score += max(0.0, 5.0 - line) * 1.5
        return score

    def _group_if_played(self, board: Board, color: int, point: Point):
        x, y = point
        board.grid[y][x] = color
        try:
            return board.group(point)
        finally:
            board.grid[y][x] = EMPTY

    @staticmethod
    def _nearest_stone_dist(board: Board, point: Point) -> Optional[int]:
        """到最近棋子的切比雪夫距离（无子时返回 None）。"""
        x, y = point
        for radius in range(1, 9):
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if max(abs(dx), abs(dy)) != radius:
                        continue
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < board.size and 0 <= ny < board.size \
                            and board.grid[ny][nx] != EMPTY:
                        return radius
        return None

    # ------------------------------------------------------------------
    # 影响力场与形势估值
    # ------------------------------------------------------------------
    def _influence_field(self, board: Board) -> list[list[float]]:
        """一次性算出全盘影响力（黑为正），后续按增量更新，避免逐点重复计算。"""
        size = board.size
        field = [[0.0] * size for _ in range(size)]
        for y in range(size):
            for x in range(size):
                v = board.grid[y][x]
                if v != EMPTY:
                    self._add_kernel(field, (x, y), 1.0 if v == BLACK else -1.0, size)
        return field

    @staticmethod
    def _add_kernel(field: list[list[float]], point: Point, sign: float, size: int,
                    radius: int = FIELD_RADIUS) -> None:
        x, y = point
        for dy in range(-radius, radius + 1):
            ny = y + dy
            if not (0 <= ny < size):
                continue
            row = field[ny]
            dy2 = dy * dy
            for dx in range(-radius, radius + 1):
                nx = x + dx
                if 0 <= nx < size:
                    row[nx] += sign / (1.0 + dx * dx + dy2)

    def _eval_from_field(self, board: Board, field: list[list[float]], komi: float,
                         prisoners_black: int = 0, prisoners_white: int = 0) -> float:
        """黑视角目差 = 空点归属份额之差 + 提子 - 贴目。

        只统计空点的控制权，不把棋子本身计为目数：否则"落一手就白得一目"，
        双方轮流下会互相抵消，导致损失目数恒为 0。
        """
        size = board.size
        black = float(board.captured_by[BLACK] + prisoners_black)
        white = float(board.captured_by[WHITE] + prisoners_white)
        for y in range(size):
            row = board.grid[y]
            frow = field[y]
            for x in range(size):
                if row[x] != EMPTY:
                    continue
                share = 0.5 + frow[x] * SHARE_WEIGHT
                share = 0.0 if share < 0.0 else (1.0 if share > 1.0 else share)
                black += share
                white += 1.0 - share
        return black - white - komi

    def _eval_after_move(self, board: Board, field: list[list[float]], komi: float,
                         color: int, point: Point) -> Optional[float]:
        """试下一手后的形势估值（增量更新影响力场，代价与候选数成线性）。

        返回 None 表示该点对这一方是自杀手，不参与估值。
        因为 root 需要「黑先下」与「白先下」两套值，同一批点对两方的合法性不同。
        """
        x, y = point
        if board.grid[y][x] != EMPTY:
            return None
        opp = other(color)
        # 1) 临时落子并算出被提的子（只改 grid，便于精确还原）
        board.grid[y][x] = color
        caps: list[Point] = []
        seen: set[Point] = set()
        for n in board.neighbors(point):
            if board.at(n) == opp and n not in seen:
                stones, libs = board.group(n)
                seen.update(stones)
                if not libs:
                    caps.extend(stones)
        for cx, cy in caps:
            board.grid[cy][cx] = EMPTY
        # 2) 自杀判定（不走 Board._check，避开全盘位置哈希的开销）
        _, my_libs = board.group(point)
        if not my_libs and not caps:
            board.grid[y][x] = EMPTY
            return None
        # 3) 影响力场增量更新
        #    新子按己方符号加入；被提的子是对方颜色，移除它们等于减去对方的贡献，
        #    所以增量与己方新子同号（写反会让提子的一方反而被扣分）
        own_sign = 1.0 if color == BLACK else -1.0
        f2 = [row[:] for row in field]
        self._add_kernel(f2, point, own_sign, board.size)
        for cp in caps:
            self._add_kernel(f2, cp, own_sign, board.size)
        # 4) 估值（提子计入俘获）
        n_caps = len(caps)
        value = self._eval_from_field(board, f2, komi,
                                      prisoners_black=n_caps if color == BLACK else 0,
                                      prisoners_white=n_caps if color == WHITE else 0)
        # 5) 还原棋盘
        board.grid[y][x] = EMPTY
        for cx, cy in caps:
            board.grid[cy][cx] = opp
        return value

    def _ownership_from_field(self, board: Board, field: list[list[float]]) -> list[float]:
        """领地热力图，直接输出**内部口径**：白方为正，下标 = y * size + x，y=0 在 GTP 第1行。

        KataGo 的原始输出是走子方为正、行序自上而下（下标行0 = GTP 第9行），
        由 protocol._convert_ownership 换算到同一口径；这里直接产出内部口径，
        两个引擎的结果才能互换而热力图不上下颠倒。
        """
        size = board.size
        out: list[float] = []
        for y in range(size):
            row = board.grid[y]
            frow = field[y]
            for x in range(size):
                v = row[x]
                if v == BLACK:
                    out.append(-1.0)
                elif v == WHITE:
                    out.append(1.0)
                else:
                    out.append(round(-math.tanh(frow[x] * OWNERSHIP_GAIN), 3))
        return out

    def status(self) -> dict:
        return {"name": self.name, "available": True, "error": ""}
