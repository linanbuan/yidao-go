"""内置题库：题目构造机制 + 眼位基本形家族（公共领域棋形知识）。

## 答案一律由搜索现场推导，不手抄

每道题只给出**摆子 + 搜索范围 + 判据 + 目标 + 先走方**（见 Spec），正解线、失败线、
对手的最佳应对全部由 solve.py 的穷举搜索算出来。这样「正解是否正确」由搜索保证，
而不是靠人对答案的记忆；tests/test_tsumego.py 会把每条线重新验证一遍。

## 出题的四道闸门（任一不过就不出这道题）

1. **深度够**：搜索撞到深度上限（ctx.truncated）说明结论不可信 → 弃题；
2. **先走能达成目标**：玩家先走的最佳结果必须恰好等于目标结论值，否则这题无解；
3. **对方先走达不成**：否则玩家无需动手，题目没有意义（这条自动排除了板六、直四
   这类无条件活形，也排除了已经死透的形状）；
4. **唯一正解**（可关）：眼位内恰好只有一手能达成目标。多个正解的题练不出计算力，
   而且「我走的也是对的」会让判定显得不可信。

棋形本身是围棋教材里的公共基本形，出处标注见 SOURCE_NOTE。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..game.rules import BLACK, Board, EMPTY, IllegalMove, Point, WHITE, other, to_gtp
from .solve import (GOAL_CAPTURE, GOAL_CONNECT, GOAL_CUT, GOAL_KILL, GOAL_KO_KILL,
                    GOAL_KO_LIVE, GOAL_LIVE, GOAL_RACE, GOAL_SEKI, Oracle,
                    SearchContext, VERDICT_BY_VALUE, capture_oracle, clone,
                    connect_oracle, enclosed_space, eye_regions, goal_text,
                    goal_value, life_oracle, local_group, player_is_protagonist,
                    race_oracle, replayable_prefix, solve, verdict_text, worse_than)

SOURCE_NOTE = (
    "基本死活形（直三/弯三/丁四/刀把五/梅花五等）与劫形（角上板六/盘角曲四等）属围棋公共知识，"
    "古典题书《玄玄棋经》(1349)、《官子谱》(1690)、《发阳论》(1713) 均为公共领域；"
    "本题库的摆子与正解由项目内的死活搜索现场推导，不含第三方受版权保护的题集内容。"
)

# 搜索深度：实测角上板六这类劫形在 18 层会撞上限（结论仍是劫，但变化图不完整），
# 24 层与 30 层结论一致；取 28 留出余量，同时用 ctx.truncated 兜底（撞上限就弃题）。
DEPTH = 28

KIND_LIFE = "life"          # 净死活（做活 / 杀棋）
KIND_KO = "ko"              # 劫活 / 劫杀
KIND_SEKI = "seki"          # 双活
KIND_RACE = "race"          # 对杀（比气）
KIND_CAPTURE = "capture"    # 吃子手筋
KIND_CONNECT = "connect"    # 连络 / 切断

KIND_TEXT = {
    KIND_LIFE: "死活",
    KIND_KO: "劫争",
    KIND_SEKI: "双活",
    KIND_RACE: "对杀",
    KIND_CAPTURE: "吃子手筋",
    KIND_CONNECT: "连络切断",
}


# ---------------------------------------------------------------------------
# 题目规格
# ---------------------------------------------------------------------------
@dataclass
class Spec:
    """一道题的完整规格。答案（lines）由 derive_problem 现场推导。"""
    pid: str
    title: str
    kind: str
    goal: str
    size: int
    setup: list[tuple[int, int, int]]      # [[x, y, color], ...]
    area: list[Point]                      # 搜索范围（全程固定，见 solve 模块 docstring）
    player: int                            # 玩家（先走方）
    protagonist: int                       # 被判定的那一方（搜索的 maximizer）
    difficulty: int
    family: str
    hint: str
    note: str
    tags: list[str] = field(default_factory=list)
    source: str = SOURCE_NOTE
    targets: list[Point] = field(default_factory=list)   # 吃子目标 / 对杀中对方的块
    own: list[Point] = field(default_factory=list)       # 对杀中主角自己的块
    unique: bool = True                    # 是否要求唯一正解第一手
    expect: str = ""                       # 期望结论（自查用，不入库）
    # 难度默认由 derive_problem 按正解线手数算（见 grade_difficulty）。
    # 手挑的模板可以关掉自动打分、直接用 difficulty 里写的数字。
    auto_difficulty: bool = True


def board_from_setup(setup, size: int, clear_ko_history: bool = False) -> Board:
    """由摆子建棋盘。

    clear_ko_history：搜索用的棋盘要清掉 place() 留下的全局同形记录，
    理由见 solve 模块 docstring（外部劫材不在盘上，留着 superko 会禁掉合法回提）。
    """
    board = Board(size)
    black = [(int(x), int(y)) for x, y, c in setup if int(c) == BLACK]
    white = [(int(x), int(y)) for x, y, c in setup if int(c) == WHITE]
    if black:
        board.place(BLACK, black)
    if white:
        board.place(WHITE, white)
    if clear_ko_history:
        board._position_hashes.clear()
        board._situational_hashes.clear()
    return board


def make_oracle(spec: Spec, board: Board) -> Oracle:
    """按**题型**装配终局判据——题型的全部差异都在这里，搜索框架完全共用。

    按 kind 而不是 goal 分派：同一题型可以考不同目标（对杀题既可以问「谁先提光对方」
    也可以问「能不能做成双活」），判据不变、只是需要的结论值变了。
    """
    area = set(spec.area)
    if spec.kind == KIND_CAPTURE:
        return capture_oracle(spec.protagonist, spec.targets)
    if spec.kind == KIND_RACE:
        return race_oracle(spec.protagonist, spec.own, other(spec.protagonist), spec.targets)
    if spec.kind == KIND_CONNECT:
        # 连络题把**要连上的两个端点**存在 own 里。复用 own 而不是新增字段，
        # 是因为 own/targets 已经入库（store.row_spec 靠它们重建判据），
        # 端点放这儿就不必再加一列迁移。
        pts = [tuple(int(v) for v in p) for p in spec.own]
        if len(pts) != 2:
            raise ValueError(f"{spec.title}: 连络题需要恰好两个端点，得到 {pts}")
        return connect_oracle(spec.protagonist, pts[0], pts[1])
    group = local_group(board, spec.protagonist, area)
    return life_oracle(spec.protagonist, area, candidates=group | area)


def probe_verdict(spec: Spec) -> Optional[str]:
    """跑一遍搜索看这道题（玩家先走）的结论；深度不够返回 None。

    对杀/吃子这类题的目标得先知道结果才能定（打出来是双活就出双活题），
    所以先探一下。探测用的 Spec 可以带一个占位 goal（只看 kind 选判据）。
    """
    board = board_from_setup(spec.setup, spec.size, clear_ko_history=True)
    oracle = make_oracle(spec, board)
    ctx = SearchContext()
    value, _ = solve(board, spec.protagonist, spec.player, spec.area, oracle,
                     depth=DEPTH, ctx=ctx)
    return None if ctx.truncated else VERDICT_BY_VALUE[value]


def verdict_line(kind: str, verdict: str, protagonist: int) -> str:
    """按**题型**给结论文案。

    同一个 "alive" 在不同题型里意思完全不同：死活题是「做出两眼」，
    对杀题是「先把对方提光」，吃子题是「把标记的子吃到了」。
    直接把 verdict_text 拿去用会把对杀题说成「黑棋已活（两个独立眼位）」。
    """
    name = "黑" if protagonist == BLACK else "白"
    rival = "白" if protagonist == BLACK else "黑"
    if kind == KIND_CAPTURE:
        return {
            "alive": f"吃到了{rival}棋的目标子",
            "ko": "只能靠打劫去吃，劫材不够就吃不到",
            "seki": "双方都动不了，吃不掉",
            "dead": f"吃不到{rival}棋的目标子",
        }.get(verdict, "")
    if kind == KIND_RACE:
        return {
            "alive": f"{rival}棋先被提光，{name}棋赢了这场对杀",
            "ko": "对杀打成劫，胜负取决于劫材",
            "seki": "双活：谁先紧气谁死，两块棋共用这几口气活下去",
            "dead": f"{name}棋先被提光，这场对杀输了",
        }.get(verdict, "")
    if kind == KIND_CONNECT:
        return {
            "alive": "两块棋连成了一块，对方再也断不开",
            "ko": "只能靠打劫去连，劫材不够就连不上",
            "seki": "谁也吃不掉谁，但两块棋始终是分开的",
            "dead": "两块棋被断开，两边各自受攻",
        }.get(verdict, "")
    return verdict_text(verdict, protagonist)


def _fmt(points, size: int) -> str:
    return "、".join(to_gtp(tuple(p), size) for p in points)


# 难度五档。分档而不只给 1~9 的数字：学员选「中级」比选「难度 5」好懂得多。
TIERS: tuple[tuple[int, str], ...] = ((2, "入门"), (3, "初级"), (5, "中级"),
                                      (7, "高级"), (9, "段位"))


def tier_of(difficulty: int) -> str:
    for upper, name in TIERS:
        if difficulty <= upper:
            return name
    return TIERS[-1][1]


def grade_difficulty(solution_moves: int, verdict: str, area_size: int,
                     solutions: int = 1) -> int:
    """难度打分：只用可测量的量，不再手填。

    旧做法是在 Shape 里写一个 difficulty 数字，它不可比（角上直三与中腹梅花五
    都可能标 3），也无法验证。改用三个客观量：

      • **正解线手数**——要把变化算到多深，这是难度的主体；
      • **结论是不是劫**——劫要额外算劫材与提劫次序，比同长度的净死净活难；
      • **area 是不是接近上限**——候选点越多，第一手越难选。

    多个正解第一手要**减分**：好几手都能成的题练不出计算力。
    """
    score = (solution_moves + 1) // 2
    if verdict == "ko":
        score += 1
    if area_size >= 8:
        score += 1
    if solutions > 1:
        score -= 1
    return max(1, min(9, score))


def derive_problem(spec: Spec) -> Optional[dict]:
    """由规格推导出一道完整的题；不成立（四道闸门任一不过）返回 None。"""
    board = board_from_setup(spec.setup, spec.size, clear_ko_history=True)
    oracle = make_oracle(spec, board)
    area = list(spec.area)
    need = goal_value(spec.goal)
    as_protag = player_is_protagonist(spec.goal)
    size = spec.size

    def judge(to_move: int) -> tuple[Optional[int], list[Point], bool, bool]:
        ctx = SearchContext()
        value, pv = solve(board, spec.protagonist, to_move, area, oracle,
                          depth=DEPTH, ctx=ctx)
        return value, pv, ctx.truncated, ctx.ko_seen

    # 闸门 1+2：玩家先走必须恰好达成目标
    first, first_pv, truncated, _ = judge(spec.player)
    if truncated:
        return None
    if first != need:
        return None
    # 闸门 3：对方先走达不成，否则无需动手
    second, _, truncated, _ = judge(other(spec.player))
    if truncated or not worse_than(second, spec.goal, as_protag):
        return None

    correct: list[dict] = []
    wrong: list[dict] = []
    for p in area:
        if board.at(p) != EMPTY or not board.is_legal(spec.player, p):
            continue
        nb = clone(board)
        try:
            nb.play(spec.player, p)
        except IllegalMove:
            continue
        ctx = SearchContext()
        value, pv = solve(nb, spec.protagonist, other(spec.player), area, oracle,
                          depth=DEPTH, ctx=ctx)
        if ctx.truncated:
            return None                       # 任何一个分支撞深度上限，整题的结论都不可信
        if value == need:
            # 存进题库的变化图必须能在真实棋盘上一手手摆出来（前端画幽灵子、
            # 接口摆失败图都直接拿它落子），涉劫时搜索给的 pv 可能含劫禁着
            safe = replayable_prefix(nb, other(spec.player), pv)
            correct.append({
                "moves": [[p[0], p[1]]],
                "pv": [[q[0], q[1]] for q in safe],
                "result": "correct",
                # 把结论存下来：api.attempt 判定「答对」时直接取它，不必对同一个局面
                # 重搜一次（实测 area=8 的题单次 ld_search 要 ~100ms，占了整个响应
                # 的九成，而框架 + 数据库只 ~13ms）。
                "verdict": VERDICT_BY_VALUE[value],
                "comment": (f"正解 {to_gtp(p, size)}："
                            f"{verdict_line(spec.kind, VERDICT_BY_VALUE[value], spec.protagonist)}。"
                            f"{spec.note}"),
            })
        else:
            safe = replayable_prefix(board, spec.player, [p] + list(pv))
            wrong.append({
                "moves": [[q[0], q[1]] for q in safe],
                "pv": [],
                "result": "wrong",
                "comment": (f"{to_gtp(p, size)} 不成立：对方 {_fmt(safe[1:], size) or '无需应手'}"
                            f" 之后{verdict_line(spec.kind, VERDICT_BY_VALUE[value], spec.protagonist)}。"),
            })

    if not correct:
        return None
    # 闸门 4：唯一正解（多个正解的题练不出计算力）
    if spec.unique and len(correct) != 1:
        return None

    verdict_name = VERDICT_BY_VALUE[first]
    if spec.expect and verdict_name != spec.expect:
        # 期望值写错比不出题更糟：直接抛出来，让测试暴露它
        raise ValueError(f"{spec.title}: 搜索结论 {verdict_name} 与期望 {spec.expect} 不符")

    # 难度取**最深的那条正解线**：学员要把变化算到那么深才能确认自己没走错。
    if spec.auto_difficulty:
        deepest = max(1 + len(line.get("pv") or []) for line in correct)
        difficulty = grade_difficulty(deepest, verdict_name, len(area), len(correct))
    else:
        difficulty = spec.difficulty

    title = spec.title
    player_name = "黑" if spec.player == BLACK else "白"
    return {
        "pid": spec.pid,
        "title": title,
        "kind": spec.kind,
        "kindText": KIND_TEXT.get(spec.kind, spec.kind),
        "shapeKey": spec.pid,
        "family": spec.family,
        "goal": spec.goal,
        "goalText": goal_text(spec.goal),
        "difficulty": difficulty,
        "tier": tier_of(difficulty),
        "size": size,
        "toMove": spec.player,
        "victim": spec.protagonist,
        "setup": [[int(x), int(y), int(c)] for (x, y, c) in spec.setup],
        "space": [[int(x), int(y)] for (x, y) in area],
        "targets": [[int(x), int(y)] for (x, y) in spec.targets],
        "own": [[int(x), int(y)] for (x, y) in spec.own],
        "hint": spec.hint,
        "note": spec.note,
        "tags": list(spec.tags) + [spec.family, KIND_TEXT.get(spec.kind, spec.kind)],
        "source": spec.source,
        "builtin": True,
        "lines": correct + wrong,
        "playerName": player_name,
    }


# ---------------------------------------------------------------------------
# 眼位形家族：只给眼位空间，包围圈自动生成
# ---------------------------------------------------------------------------
@dataclass
class Shape:
    key: str
    title: str
    space: list[Point]          # 眼位空间（受害方内部的空点）
    difficulty: int             # 1~5
    family: str                 # 边上 / 角上 / 中腹
    hint: str
    note: str                   # 正解讲解
    size: int = 9
    victim: int = BLACK
    tags: list[str] = field(default_factory=list)
    goals: tuple[str, ...] = (GOAL_LIVE, GOAL_KILL)
    unique: bool = True
    expect: dict = field(default_factory=dict)   # goal → 期望结论（自查）
    # 外气点（area 内、眼位空间外）。空着就是零外气——旧行为完全不变。
    # 外气是教材里结论的分水岭（「六目角：没外气死、一口外气劫、两口外气活」），
    # 也是旧家族出不了难题的根本原因：validate_setup 曾要求气全落在眼位内。
    outside_libs: list[Point] = field(default_factory=list)


# 眼位空间用内部坐标 (x, y)，y=0 是 GTP 第 1 行（棋盘底部）。
#
# 这张表里的每个形状都经过穷举搜索验证（tests/test_tsumego.py 会再验一遍）。
# expect 写的是**教材公认结论**，搜索跑出来不一样就抛错——这是把「搜索引擎」
# 和「教材口径」钉在一起的锚点，改判定逻辑时它第一个报警。
#
# 被筛掉、不放进这张表的形状：
#   板六 / 直四 —— 无条件活形，对方先走也杀不掉，出题没有意义（闸门 3 会自动拒）
#   方四 —— 自己先走也活不了，没有正解（闸门 2 会自动拒）
SHAPES: list[Shape] = [
    # ---- 直三：急所永远在正中 ----
    Shape("straight-three-corner", "角上直三", [(0, 0), (1, 0), (2, 0)], 1, "角上",
          "三个连排空点，急所在正中。",
          "直三的正解只有一处：正中。走成两个独立的一目眼，对方再也点不进来。",
          expect={GOAL_LIVE: "alive", GOAL_KILL: "dead"}),
    Shape("straight-three-edge", "边上直三", [(1, 0), (2, 0), (3, 0)], 1, "边上",
          "三个连排空点，急所在正中。",
          "边上直三同理：占正中即成两眼，走两端反而只留一个眼位。",
          expect={GOAL_LIVE: "alive", GOAL_KILL: "dead"}),
    Shape("straight-three-center", "中腹直三", [(3, 3), (4, 3), (5, 3)], 2, "中腹",
          "中腹也是同一个道理：三目正中。",
          "中腹直三：四面受敌，但眼位形状与边上完全一样，仍是正中一点。",
          expect={GOAL_LIVE: "alive", GOAL_KILL: "dead"}),

    # ---- 弯三：急所在拐点 ----
    Shape("bent-three-corner", "角上弯三", [(0, 0), (1, 0), (0, 1)], 1, "角上",
          "拐弯形的急所在拐角那一点。",
          "角上弯三：占住拐点（角上那一点）即分出一目眼，另一侧再做一目。",
          expect={GOAL_LIVE: "alive", GOAL_KILL: "dead"}),
    Shape("bent-three-edge", "边上弯三", [(1, 0), (2, 0), (2, 1)], 2, "边上",
          "拐弯形的急所在拐角那一点。",
          "弯三与直三同理：占住拐点即成两眼，其余任何一点都只留一个眼位。",
          expect={GOAL_LIVE: "alive", GOAL_KILL: "dead"}),
    Shape("bent-three-center", "中腹弯三", [(3, 3), (4, 3), (4, 4)], 3, "中腹",
          "找到拐弯的那一点。",
          "中腹弯三：拐点一子把三目分成 1+1，两眼即成。",
          expect={GOAL_LIVE: "alive", GOAL_KILL: "dead"}),

    # ---- 丁四：急所在交叉点 ----
    Shape("t-four-corner", "角上丁四", [(0, 0), (1, 0), (2, 0), (1, 1)], 2, "角上",
          "丁字形的急所在交叉点（不是凸出的那一点）。",
          "角上丁四：交叉点一子把空间切成三块，任选两块即两眼。",
          expect={GOAL_LIVE: "alive", GOAL_KILL: "dead"}),
    Shape("t-four-edge", "边上丁四", [(1, 0), (2, 0), (3, 0), (2, 1)], 3, "边上",
          "丁字形的急所在交叉点。",
          "边上丁四：占住交叉点后形成三个独立眼位，对方无法同时破掉两个。",
          expect={GOAL_LIVE: "alive", GOAL_KILL: "dead"}),

    # ---- 梅花五：急所在正中 ----
    Shape("plum-five-edge", "边上梅花五", [(2, 0), (1, 1), (2, 1), (3, 1), (2, 2)], 3, "边上",
          "梅花五的急所在正中央。",
          "梅花五点中央后剩四个散点，对方任何一子都做不出第二个眼。",
          expect={GOAL_LIVE: "alive", GOAL_KILL: "dead"}),
    Shape("plum-five-center", "中腹梅花五", [(4, 3), (3, 4), (4, 4), (5, 4), (4, 5)], 4, "中腹",
          "五目梅花，中心即急所。",
          "中腹梅花五：中心一点把五目分成四个一目，聚杀的经典形。",
          expect={GOAL_LIVE: "alive", GOAL_KILL: "dead"}),

    # ---- 葡萄六：只做杀棋题（做活有多个正解，不适合当唯一答案的练习题）----
    Shape("grape-six-corner", "角上葡萄六", [(0, 0), (0, 1), (1, 0), (1, 1), (1, 2), (2, 1)],
          5, "角上", "六目聚形也有唯一急所，不在几何中心。",
          "角上葡萄六（聚六）只有一个点能杀，走错黑就能做出两眼。",
          goals=(GOAL_KILL,), expect={GOAL_KILL: "dead"}),

    # ---- 劫形：结论是「劫」，先走方决定它是劫活还是劫杀 ----
    # 角上板六（无外气）：教材口径「白先活、黑先劫」。搜索复现了这条。
    # 这一类形状已经枚举穷尽：角部 3~7 目共 187 个规范形里只有 4 个是劫
    # （下面手挑的三个 + ko_shape_specs 找到的七目形），而且全是「守先净活、
    # 攻先成劫」；边上 223 个规范形里一个劫形也没有。本家族的包围圈把守方的气
    # 全关在眼位里（见 validate_setup），而劫活需要外气或更大的眼位，
    # 所以这里出不出「黑先劫活」题是形状家族的性质，不是搜索漏了。
    Shape("corner-six", "角上板六", [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)],
          4, "角上", "角上板六无外气时，点入只能打成劫。",
          "角上板六（无外气）：点入后形成劫争，谁先提劫谁掌握主动，胜负取决于劫材。"
          "同样形状在边上（有一口外气）就是净活——差别全在外气。",
          goals=(GOAL_KO_KILL,), expect={GOAL_KO_KILL: "ko"}),
    # 盘角曲四：局部是劫，规则上「劫尽棋亡」判死。这里按局部结论出「劫杀」题，
    # 讲解里把规则结论讲清楚，避免学员被两种说法绕晕。
    Shape("corner-bent-four", "盘角曲四", [(0, 0), (0, 1), (0, 2), (1, 0)],
          5, "角上", "曲四在角上不等于活，点入成劫。",
          "盘角曲四：局部是劫，但守方在角上找不到劫材，规则上判「劫尽棋亡」＝死。"
          "这是围棋规则里最著名的特例之一，务必记住形状。",
          goals=(GOAL_KO_KILL,), expect={GOAL_KO_KILL: "ko"}),
    Shape("corner-l-six", "角上曲六", [(0, 0), (0, 1), (0, 2), (0, 3), (1, 0), (1, 1)],
          5, "角上", "六目但拐了个弯，急所不在中间。",
          "角上曲六（无外气）与角上板六同理：点入成劫，守方靠打劫争活。",
          goals=(GOAL_KO_KILL,), expect={GOAL_KO_KILL: "ko"}),
]


def _neighbors(point: Point, size: int) -> list[Point]:
    x, y = point
    return [(nx, ny) for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))
            if 0 <= nx < size and 0 <= ny < size]


def _rim(points: set[Point], size: int) -> set[Point]:
    """points 的四邻点集合（限制在棋盘内）。"""
    out: set[Point] = set()
    for p in points:
        out.update(_neighbors(p, size))
    return out


def _components(points: set[Point], size: int) -> list[set[Point]]:
    comps: list[set[Point]] = []
    rest = set(points)
    while rest:
        seed = rest.pop()
        comp = {seed}
        stack = [seed]
        while stack:
            cur = stack.pop()
            for n in _neighbors(cur, size):
                if n in rest:
                    rest.discard(n)
                    comp.add(n)
                    stack.append(n)
        comps.append(comp)
    return comps


def _bridge(a: set[Point], b: set[Point], blocked: set[Point], size: int) -> list[Point]:
    """在非 blocked 的空点上找 a→b 的最短连接路径（BFS），返回路径上的中间点。"""
    from collections import deque

    start_states = [(p, []) for p in a]
    queue = deque(start_states)
    seen = {p for p, _ in start_states}
    while queue:
        cur, path = queue.popleft()
        for n in sorted(_rim({cur}, size)):
            if n in b:
                return path + [n]
            if n in seen or n in blocked:
                continue
            seen.add(n)
            queue.append((n, path + [n]))
    return []


def build_setup(shape: Shape) -> tuple[list[tuple[int, int, int]], Board]:
    """由眼位空间生成完整摆子：受害方的墙（自动连通）+ 攻方的外侧封锁。

    保证：受害方的气恰好等于「眼位空间 + 声明的外气点」（否则题目不成立——它可以往外逃）。
    """
    space = set(shape.space)
    # 注意减掉 space 自身：空间内部的点互为四邻，不减就会把眼位填成受害方的子
    victim = _rim(space, shape.size) - space
    # 把受害方的断点连起来，图形才像教材里的死活题（否则是几颗孤子）
    for _ in range(8):
        comps = _components(victim, shape.size)
        if len(comps) <= 1:
            break
        comps.sort(key=len, reverse=True)
        merged = False
        for i in range(len(comps)):
            for j in range(i + 1, len(comps)):
                path = _bridge(comps[i], comps[j], space, shape.size)
                if path:
                    victim |= set(path)
                    merged = True
                    break
            if merged:
                break
        if not merged:
            break
    attacker = _rim(victim, shape.size) - space - victim

    # 外气：从攻方集合里挖掉声明的点，它们就成了受害方的外气。
    # **必须再把这些点背向受害方的一侧封死**：那些点不贴墙、也就不在 rim(victim) 里，
    # 根本不会自动生成攻方子，留着就是空点——落在外气点上的子在 area 外永远有气、
    # 谁都吃不掉，等于一次白得的 pass（与 puzzles._race_spec 记录的「中立点」病同源）。
    # 实测：不封时「角上直三 + 1 外气」会算出假的 seki。
    outside = set(shape.outside_libs)
    if outside:
        attacker -= outside
        for lib in outside:
            for n in _rim({lib}, shape.size):
                if n in outside or n in space or n in victim:
                    continue
                attacker.add(n)

    board = Board(shape.size)
    board.place(shape.victim, sorted(victim))
    board.place(other(shape.victim), sorted(attacker))
    setup = ([(x, y, shape.victim) for (x, y) in sorted(victim)]
             + [(x, y, other(shape.victim)) for (x, y) in sorted(attacker)])
    return setup, board


def validate_setup(shape: Shape, board: Board) -> None:
    """摆子必须满足题目前提，否则这道题的判定没有意义。

    三条前提：①受害方恰好只有一个眼位区域（= 给定空间）；②受害方的气全部
    落在「眼位空间 + 声明的外气点」里（否则它可以往 area 外逃，搜索算不到）；
    ③实际外气与声明的完全相符。

    注意①里用的是 eye_regions，它只算**被受害方完全围住**的空区，所以外气点
    （贴着攻方子）永远不会出现在里面——不能拿「区域并集 == 空间 + 外气」做判据
    （实测这么写会把所有外气变体全部误拒）。外气只能靠②③的气检查来验。

    气可以比空间少：像丁四、梅花五这类形状，空间里有些点四周全是空点，
    它们不是任何棋块的气，但仍然是眼位的一部分。
    """
    regions = eye_regions(board, shape.victim)
    if len(regions) != 1:
        raise ValueError(f"{shape.title}: 受害方眼位区域数={len(regions)}，应为 1")
    if set(regions[0]) != set(shape.space):
        raise ValueError(f"{shape.title}: 眼位区域与给定空间不一致")
    want = set(shape.space) | set(shape.outside_libs)

    victim_pts = {(x, y) for y in range(board.size) for x in range(board.size)
                  if board.at((x, y)) == shape.victim}
    libs: set[Point] = set()
    for comp in _components(victim_pts, shape.size):
        _, liberties = board.group(next(iter(comp)))
        libs |= set(liberties)
    if not libs:
        raise ValueError(f"{shape.title}: 受害方没有气")
    if not libs <= want:
        raise ValueError(f"{shape.title}: 受害方的气 {sorted(libs)} 超出了范围 {sorted(want)}")
    actual_outside = libs - set(shape.space)
    if actual_outside != set(shape.outside_libs):
        raise ValueError(f"{shape.title}: 实际外气 {sorted(actual_outside)} 与声明的 "
                         f"{sorted(shape.outside_libs)} 不符")


GOAL_KIND = {
    GOAL_LIVE: KIND_LIFE, GOAL_KILL: KIND_LIFE,
    GOAL_KO_LIVE: KIND_KO, GOAL_KO_KILL: KIND_KO,
    GOAL_SEKI: KIND_SEKI, GOAL_RACE: KIND_RACE, GOAL_CAPTURE: KIND_CAPTURE,
    GOAL_CONNECT: KIND_CONNECT, GOAL_CUT: KIND_CONNECT,
}


def shape_to_spec(shape: Shape, goal: str) -> Spec:
    """眼位形 → 题目规格（摆子自动生成，搜索范围 = 眼位空间）。"""
    setup, board = build_setup(shape)
    validate_setup(shape, board)
    victim = shape.victim
    # 做活/劫活/双活：玩家就是被判定的那一方；杀棋/劫杀：玩家是攻方
    player = victim if player_is_protagonist(goal) else other(victim)
    # 搜索范围 = 眼位空间 + 外气点。外气点必须算进 area：它是双方都要争的一手
    # （攻方填它就是在紧气），不包含进来就等于把它当成了 area 外的无限气。
    space = sorted(set(enclosed_space(board, victim)) | set(shape.outside_libs))
    title_goal = {
        GOAL_LIVE: "先做活", GOAL_KILL: "先杀棋",
        GOAL_KO_LIVE: "先劫活", GOAL_KO_KILL: "先劫杀",
        GOAL_SEKI: "先做双活",
    }[goal]
    who = "黑" if player == BLACK else "白"
    return Spec(
        pid=f"{shape.key}-{goal}",
        title=f"{shape.title}·{who}{title_goal}",
        kind=GOAL_KIND[goal],
        goal=goal,
        size=shape.size,
        setup=setup,
        area=space,
        player=player,
        protagonist=victim,
        difficulty=shape.difficulty + (0 if player_is_protagonist(goal) else 1),
        family=shape.family,
        hint=shape.hint,
        note=shape.note,
        tags=list(shape.tags) + ["基本形"],
        unique=shape.unique,
        expect=shape.expect.get(goal, ""),
    )


def build_problem(shape: Shape, goal: str) -> dict:
    """生成一道题（含摆子、正解线、失败线）。goal: live=做活 / kill=杀棋 / ko_kill=劫杀 …

    保留这个入口是为了兼容旧调用方与旧测试；新代码直接用 derive_problem。
    """
    problem = derive_problem(shape_to_spec(shape, goal))
    if problem is None:
        raise ValueError(f"{shape.title}（{goal}）不成立，不出此题")
    return problem


def build_shape_library() -> list[dict]:
    """眼位形家族的全部题目（不成立的形状/目标自动跳过）。"""
    out: list[dict] = []
    for shape in SHAPES:
        for goal in shape.goals:
            problem = derive_problem(shape_to_spec(shape, goal))
            if problem is not None:
                out.append(problem)
    return out


def build_library() -> list[dict]:
    """全部内置题目 = 眼位形家族 + 对杀/吃子/带外气劫形（见 puzzles.py）。"""
    from .puzzles import build_puzzle_library

    return build_shape_library() + build_puzzle_library()


__all__ = ["DEPTH", "GOAL_CAPTURE", "GOAL_CONNECT", "GOAL_CUT", "GOAL_KILL",
           "GOAL_KO_KILL", "GOAL_KO_LIVE", "GOAL_LIVE", "GOAL_RACE", "GOAL_SEKI",
           "KIND_CAPTURE", "KIND_CONNECT", "KIND_KO", "KIND_LIFE", "KIND_RACE",
           "KIND_SEKI", "KIND_TEXT", "SHAPES", "SOURCE_NOTE", "TIERS", "Shape",
           "Spec", "board_from_setup", "build_library", "build_problem",
           "build_setup", "build_shape_library", "derive_problem", "grade_difficulty",
           "make_oracle", "probe_verdict", "shape_to_spec", "tier_of",
           "validate_setup", "verdict_line"]
