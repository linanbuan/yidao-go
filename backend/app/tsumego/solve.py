"""死活判定核心：眼位 / 目标识别 + 局部穷举搜索，四种结论（净活 / 双活 / 劫 / 净死）。

为什么自己判而不用 KataGo：死活题的结论必须是**确定的**，而引擎给的是胜率与目差——
「大优」不等于「已活」，用它判题只能得到含糊结论（点错一手只掉几个百分点，判不出「死」）。
本地搜索的结论与教科书一致，而且搜索路径本身就是变化图（pv），可以直接给前端演示。

## 四种结论

以「被判定的那一方」（下称主角 / maximizer）视角排序，数值越大对主角越好：

    净死 DEAD(0) < 劫 KO(1) < 双活 SEKI(2) < 净活 ALIVE(3)

主角最大化、对手最小化，于是「劫」和「双活」能作为**独立结论**参与 minimax：
攻方若既能净杀又能打成劫，它选净杀（0 < 1）；守方若既能净活又只能打劫，它选净活。
这正是旧实现缺的东西——只有 alive/dead 两值时，任何涉劫的形状都得整题丢掉。

## 劫是怎么算出来的（本次升级的核心）

局部搜索不允许在区域外落子，所以「到别处找劫材」必须被抽象掉，否则被提劫的一方
会因为无处可下而直接被判死——这就是旧实现「遇到劫就拒题」的根因。这里的模型是
**外部劫材无限**：

  1. 一方被劫禁着挡住时，**无论局部还有没有其它应手**，都可以「到别处找劫材」，
     对方必须应（不应就等于连挨两手），于是着法权回到自己手上、劫禁着解除；
  2. 双方轮流提劫 → 局面重复 → 由**循环检测**判为「劫」。

「有局部应手时也得允许找劫材」这一条不能省：否则被提劫的一方会被迫在自己眼位里
自填，把标准劫形（角上板六）算成净死。找劫材也不是白得的先手：盘面不变、
着法权仍归自己，只是把劫禁着解除。

与教材口径一致：角上板六（无外气）不论谁先都是「劫」，至于是劫活还是劫杀，
由题目的先走方决定，而不是靠搜索去猜劫材多少。

循环里**没有提子**的重复判为「双活」：双方都无处可下、谁先紧气谁死，教材结论是共活。
它与「劫」必须分开——两者的教学价值完全不同。

## 同形规则只用简单劫

搜索内部清掉全局同形记录（superko），只保留 board.ko_point 的简单劫禁着。
因为「外部劫材」不落在这块棋盘上，留着 superko 会把找过劫材之后的**合法回提**一并
禁掉，于是劫活又被算成净死。循环本身由上面的循环检测负责，不需要 superko 兜底。

## 搜索范围 area 全程固定

落子只考虑 area 内的空点。为什么不能每层重算「被主角包围的空点」：攻方一旦点入，
剩下的空点就不再是「纯被主角包围」的区域，重算会得到「没有眼位」并直接判死——
于是任何点入都显得致命（实测：边上直三的杀棋题会算出 B1/C1/D1 三个「正解」）。

## 缓存与循环的相容性（memo 的正确性）

有循环的博弈里，同一个局面的值**可能与到达它的路径有关**（循环检测要看祖先）。
所以只缓存「子树里没有指向上层祖先的循环」的结果：_search 额外返回一个 anchor
（被重复的那个祖先状态键），沿途节点一律不缓存，等回到 anchor 自身时才清掉并缓存——
该节点的子树已包含整个循环，换条路径到达它也会重演同样的循环，所以它的值与路径无关。
一个子树里出现多个 anchor 时上交**最外层**那个：上交内层的会让外层节点错误入缓存。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from ..game.rules import BLACK, Board, EMPTY, IllegalMove, Point, WHITE, other

# ---------------------------------------------------------------------------
# 结论值与目标
# ---------------------------------------------------------------------------
DEAD, KO, SEKI, ALIVE = 0, 1, 2, 3

VERDICT_BY_VALUE: dict[int, str] = {DEAD: "dead", KO: "ko", SEKI: "seki", ALIVE: "alive"}
VERDICT_VALUE: dict[str, int] = {name: value for value, name in VERDICT_BY_VALUE.items()}

Verdict = str      # "alive" | "seki" | "ko" | "dead"

# 目标 → (中文名, 需要的结论值, 玩家是不是主角)
# 「主角」= 被判定的那一方 = maximizer。做活/劫活/双活/吃子/连通类题目里玩家就是主角，
# 杀棋/劫杀类题目里玩家是攻方（minimizer），结论值仍按主角视角记，所以 kill 要的是 DEAD。
GOAL_LIVE = "live"
GOAL_KILL = "kill"
GOAL_KO_LIVE = "ko_live"
GOAL_KO_KILL = "ko_kill"
GOAL_SEKI = "seki"
GOAL_CAPTURE = "capture"
GOAL_RACE = "race"
GOAL_CONNECT = "connect"
GOAL_CUT = "cut"

GOALS: dict[str, tuple[str, int, bool]] = {
    GOAL_LIVE: ("做活", ALIVE, True),
    GOAL_KILL: ("杀棋", DEAD, False),
    GOAL_KO_LIVE: ("劫活", KO, True),
    GOAL_KO_KILL: ("劫杀", KO, False),
    GOAL_SEKI: ("双活", SEKI, True),
    GOAL_CAPTURE: ("吃子", ALIVE, True),
    GOAL_RACE: ("对杀", ALIVE, True),     # 主角 = 玩家自己那块，ALIVE 意为「对方先被提光」
    # 连络题：玩家就是要把自己两块连上的那一方，连成一块 = ALIVE。
    GOAL_CONNECT: ("连络", ALIVE, True),
    # 切断题是它的镜像：主角 = 对方（想连的那一方），玩家要它**连不上**。
    # 达成值用 SEKI 而不是 DEAD：本模板（一线缺口）里切断的结局是双方僵持的
    # 双活（SEKI）——谁也别动，缺口被占死、两块棋永远分开；提死对方那种
    # DEAD 结局在缺口模板里不存在。「恰好达成」的语义要求 need 与真实结局一致，
    # 用 DEAD 会把「已经切断成功」的题全部拒掉（L3 实测）。
    GOAL_CUT: ("切断", SEKI, False),
}


def goal_text(goal: str) -> str:
    return GOALS.get(goal, (goal,))[0]


def goal_value(goal: str) -> int:
    """达成该目标所需的结论值（主角视角）。"""
    return GOALS[goal][1]


def player_is_protagonist(goal: str) -> bool:
    return GOALS[goal][2]


def goal_achieved(verdict: Verdict, goal: str) -> bool:
    """结论是否恰好达成目标。

    用「恰好」而不是「至少」：做活题只做成双活不算成功（教材口径就是要两眼），
    劫活题净活了说明这道题标错了目标——出题时已经排除这种情况。
    """
    if not verdict:
        return False
    return VERDICT_VALUE.get(verdict, -1) == goal_value(goal)


def worse_than(value: int, goal: str, as_protagonist: bool) -> bool:
    """从玩家视角看，这个结论值是否**达不成**目标。"""
    need = goal_value(goal)
    return value < need if as_protagonist else value > need


# ---------------------------------------------------------------------------
# 棋盘工具
# ---------------------------------------------------------------------------
def all_points(board: Board) -> list[Point]:
    return [(x, y) for y in range(board.size) for x in range(board.size)]


def stones_of(board: Board, color: int) -> list[Point]:
    return [p for p in all_points(board) if board.at(p) == color]


def clone(board: Board) -> Board:
    """整盘复制（含同形记录），用于搜索分支。"""
    b = Board(board.size, board.ko_rule)
    b.restore(board.snapshot())
    return b


def empty_regions(board: Board) -> list[tuple[list[Point], set[int]]]:
    """把空点按连通性分区，返回 [(区域点, 相邻颜色集合)]。"""
    seen: set[Point] = set()
    out: list[tuple[list[Point], set[int]]] = []
    for p in board.empty_points():
        if p in seen:
            continue
        region: list[Point] = []
        borders: set[int] = set()
        stack = [p]
        seen.add(p)
        while stack:
            cur = stack.pop()
            region.append(cur)
            for n in board.neighbors(cur):
                v = board.at(n)
                if v == EMPTY:
                    if n not in seen:
                        seen.add(n)
                        stack.append(n)
                else:
                    borders.add(v)
        out.append((region, borders))
    return out


def eye_regions(board: Board, color: int) -> list[list[Point]]:
    """完全被 color 包围的空点区域（= color 的眼位候选）。"""
    return [sorted(region) for region, borders in empty_regions(board)
            if borders == {color}]


def enclosed_space(board: Board, color: int) -> list[Point]:
    """color 全部眼位区域的并集，按 y、x 排序（搜索的落点范围）。"""
    pts: set[Point] = set()
    for region in eye_regions(board, color):
        pts.update(region)
    return sorted(pts)


def eyes_in_area(board: Board, victim: int, area: Iterable[Point]) -> list[list[Point]]:
    """落在 area 内的受害方眼位区域（不区分真眼假眼，仅供展示与旧调用方使用）。

    判活请用 true_eyes_in_area：本函数只看「区域的四邻是不是全是己方颜色」，
    而这些子可能分属互不相连的几块棋——那就是假眼。
    """
    want = set(area)
    return [r for r in eye_regions(board, victim) if set(r) & want]


def true_eyes_in_area(board: Board, color: int, area: Iterable[Point]) -> list[list[Point]]:
    """area 内「保得住」的眼位区域：完全被 color 包围，且区域大小 ≤ 2 点。

    为何限制 ≤2 点（而不是只要封闭就算眼）：
      • 1 点：对方下进来就是自杀（除非能吃子，而能吃子意味着它本身已经不成立）；
      • 2 点：对方下进来只剩一口气，己方随时补另一点就能提掉，提完还原成 1 点眼；
      • 3 点（直三）：对方点中间后己方要**连补两手**才能提干净，而这两手期间
        对方可以去破另一个眼位——两个直三区域并不等于活。
    所以「两个 ≤2 点的封闭区域」才是可以立即宣布净活的充分条件。

    为何**不**要求包围圈连成一块：曾试过加这条，结果把「角上弯三」误判成死——
    那里两个眼位由角上一颗孤子与外侧墙共同围出（不连通），但对方两个眼都填不进去
    （填了就是自杀），实际是净活。假眼的真正判据是「包围的子能被分头吃掉」，
    而不是「不连通」，而前者已经被「每组棋至少两口气」与搜索本身覆盖。

    实测：本判据与「不限大小的封闭区域」在全部角部/边上/中腹枚举形上结论一致。
    """
    return [r for r in eyes_in_area(board, color, area) if len(r) <= 2]


def connected(board: Board, color: int, a: Point, b: Point) -> bool:
    """a、b 两点上的同色棋是否已连成一块。"""
    if board.at(a) != color or board.at(b) != color:
        return False
    stones, _ = board.group(a)
    return b in set(stones)


def local_group(board: Board, color: int, area: Iterable[Point]) -> set[Point]:
    """与 area 相邻的 color 棋块（多块则取并集）——死活题里被判定的就是这块棋。

    为何不用「全盘该色的子」：玩家脱先在远处落一子时，那颗子永远提不掉，
    于是「整块被提 = 净死」的终局条件再也不成立，搜索会一路走到双方无处可下
    而误报「双活」（实测：做活题脱先后得到 seki 而不是 dead）。
    """
    seeds: set[Point] = set()
    for p in area:
        for n in board.neighbors(p):
            if board.at(n) == color:
                seeds.add(n)
    out: set[Point] = set()
    for s in seeds:
        stones, _ = board.group(s)
        out |= set(stones)
    return out


def _ko_blocked(board: Board, color: int, point: Point) -> bool:
    """该点是否「仅因劫 / 同形规则」被禁（自杀不算）。

    为何不用 board.ko_point 直接当判据：它只表示「单子提单子」，普通的扑与提子也会
    触发（实测：角上刀把五的杀棋变化里白 A2 提黑 A1 就被误标成劫），把一整类正常
    题目全部误杀。真正影响搜索的只有「同形禁着」：它会让本可提劫的一方失去应手。
    """
    saved = (board.ko_point, board._position_hashes, board._situational_hashes)
    board.ko_point = None
    board._position_hashes = set()
    board._situational_hashes = set()
    try:
        return board.is_legal(color, point)
    finally:
        board.ko_point, board._position_hashes, board._situational_hashes = saved


# ---------------------------------------------------------------------------
# 终局判据（题型 = 判据；搜索框架完全共用）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Oracle:
    """给定棋盘返回结论值（主角视角），None 表示尚未终局。

    last_capture 是「导致当前局面的那一手」提掉的点，吃子 / 对杀类判据需要它来
    区分「目标块被提光」与「弃子」。

    exhausted 是撞深度上限时的兜底值。它只影响中间节点的剪枝：调用方会用
    ctx.truncated 把这类题整个丢掉，所以兜底值不会变成对外给出的答案。
    """
    evaluate: Callable[[Board, frozenset], Optional[int]]
    exhausted: int = DEAD


def life_oracle(protagonist: int, area: Iterable[Point],
                candidates: Optional[Iterable[Point]] = None) -> Oracle:
    """死活判据：被判定棋块提光 = 净死；做出两个真眼 = 净活。

    candidates 限定「主角的子可能出现在哪些点」（被判定的那块棋 + area）。
    它既是性能手段（免得每个节点全盘扫 81 个点），也是**正确性**手段：
    脱先落在远处的那颗子不属于被判定棋块，算进去会让「被提光」永远不成立。
    不传则退化为全盘扫描（调用方自己保证盘上没有无关的同色子）。
    """
    want = frozenset(area)
    where = frozenset(candidates) if candidates is not None else None

    def evaluate(board: Board, last_capture: frozenset) -> Optional[int]:
        if where is None:
            if not stones_of(board, protagonist):
                return DEAD
        elif not any(board.at(p) == protagonist for p in where):
            return DEAD
        if len(true_eyes_in_area(board, protagonist, want)) >= 2:
            return ALIVE
        return None

    return Oracle(evaluate)


def race_oracle(protagonist: int, own_group: Iterable[Point],
                rival: int, rival_group: Iterable[Point]) -> Oracle:
    """对杀（比气）判据：自己的目标块被提光 = 输；对方的先被提光 = 赢。

    双活由搜索的循环检测给出（双方都无处可下、谁紧气谁死 → SEKI），
    这正是对杀题里最有教学价值的一类结论。
    """
    mine = tuple(own_group)
    theirs = tuple(rival_group)

    def evaluate(board: Board, last_capture: frozenset) -> Optional[int]:
        if not any(board.at(p) == protagonist for p in mine):
            return DEAD
        if not any(board.at(p) == rival for p in theirs):
            return ALIVE
        return None

    return Oracle(evaluate, exhausted=DEAD)


def capture_oracle(protagonist: int, targets: Iterable[Point],
                   need_all: bool = False) -> Oracle:
    """吃子手筋判据：目标子被提 = 成功。

    need_all=False（默认）：提掉任意一颗目标子即算成功，对应「吃住这几子」的题意，
    倒扑 / 接不归 / 枷 都是这个口径。
    need_all=True：必须全部提光才算成功。
    """
    pts = tuple(targets)

    def evaluate(board: Board, last_capture: frozenset) -> Optional[int]:
        if not pts:
            return None
        hit = last_capture & set(pts) if last_capture else set()
        if hit and (not need_all or not any(board.at(p) == protagonist for p in pts)):
            return ALIVE
        if need_all and not any(board.at(p) == protagonist for p in pts):
            return ALIVE
        return None

    return Oracle(evaluate, exhausted=DEAD)


def connect_oracle(protagonist: int, a: Point, b: Point) -> Oracle:
    """连接判据：两点上的己方棋连成一块 = 成功；有一头被提 = 失败。

    目前还没有题型用它出题（library.KIND_* 里没 connect），保留是因为它是
    「题型 = 判据」这个设计里缺的最后一块，而且 tests/test_tsumego.py 钉住了它的语义。
    真要出连接题得注意：这里要的是**严格连通**（同一块棋），所以尖/飞这类
    「断不开但也没连上」的形状得靠搜索把它走成真连通才算成功，不能直接把
    第一手当正解。
    """

    def evaluate(board: Board, last_capture: frozenset) -> Optional[int]:
        if board.at(a) != protagonist or board.at(b) != protagonist:
            return DEAD
        return ALIVE if connected(board, protagonist, a, b) else None

    return Oracle(evaluate, exhausted=DEAD)


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------
@dataclass
class SearchContext:
    """一次搜索的过程状态：路径（循环检测）+ 诊断标志。"""
    path_keys: list[tuple] = field(default_factory=list)          # 根到当前节点的状态键
    path_index: dict[tuple, int] = field(default_factory=dict)    # 状态键 → 在 path_keys 的下标
    path_captured: list[bool] = field(default_factory=list)       # 每一步是否提过子
    truncated: bool = False       # 撞到深度上限 → 结论不可信，调用方必须弃题
    ko_seen: bool = False         # 变化中出现过劫
    seki_seen: bool = False       # 变化中出现过双活
    nodes: int = 0                # 展开的节点数（性能与体量自查用）


# 变化图里的「脱先」占位。用字符串而不是 None：None 已经被「找劫材的虚手」用了
# （那种虚手不占一手、不入 pv），两者语义不同，混用会让 pv 的黑白交替错位。
_PASS = "pass"


def _state_key(board: Board, to_move: int) -> tuple:
    """状态 = 盘面 + 走子方 + 劫禁着点。

    必须带劫禁着点：提劫之后的局面与提劫之前可能同盘同走子方，只有禁着点不同，
    少了它循环检测会把「刚提完劫」误判成「已经循环过」。
    """
    return (tuple(v for row in board.grid for v in row), to_move, board.ko_point)


def _local(board: Board) -> Board:
    """搜索专用副本：清掉全局同形记录（理由见模块 docstring）。"""
    nb = clone(board)
    nb._position_hashes.clear()
    nb._situational_hashes.clear()
    return nb


def _after_move(board: Board, color: int, point: Point) -> tuple[Board, list[Point]]:
    """落子后的搜索副本，返回 (新棋盘, 被提的点)。非法手抛 IllegalMove。

    提子的点必须往外传：吃子类判据靠它区分「提掉目标子」与「弃子」，
    只看盘面分不出来（弃子之后盘面也可能恰好没有目标色的子）。
    """
    nb = _local(board)
    captured = nb.play(color, point)
    nb._position_hashes.clear()
    nb._situational_hashes.clear()
    return nb, captured


def _ko_threat(board: Board) -> Board:
    """「到别处找劫材、对方应一手」的抽象：盘面不变、劫禁着解除。

    着法权仍归找劫材的一方——对方若不应，就要在别处连挨两手，代价远大于应一手，
    所以「必须应」是围棋里的常识，可以直接当成规则用。
    """
    nb = _local(board)
    nb.ko_point = None
    return nb


def solve(board: Board, protagonist: int, to_move: int, area: Iterable[Point],
          oracle: Oracle, depth: int = 24,
          ctx: Optional[SearchContext] = None,
          memo: Optional[dict] = None) -> tuple[int, list[Point]]:
    """穷举判定，返回 (结论值, 关键变化图)。结论值按主角视角，见模块 docstring。

    pv 只含**真实落子**，而且黑白严格交替，前端可以直接按交替画幽灵子：
      • 「到别处找劫材」的虚手不入 pv（盘面不变、着法权也不变，去了不影响交替）；
      • 「局部无处可下只能脱先」会入 pv 占位，对外表示为 **TENUKI 哨兵 (-1, -1)**（不再截断，L3）。
        不截断就会错位：脱先节点交上来的 pv 是以**对方**的手开头的，而上一级
        会把自己的手拼在前面，于是同一色连下两手（实测：角上板六的失败线里
        出现了自杀手与重复点，回放直接抛 IllegalMove）。
        **L3 起不再截断**：pv 以 TENUKI 哨兵 (-1,-1) 保留脱先（回放跳过落子但
        著法权照翻），同色连手的错位问题由哨兵占位解决；截断会丢掉脱先之后
        的真实内容（如连络题的「连上第二头」）。
    """
    if ctx is None:
        ctx = SearchContext()
    if memo is None:
        memo = {}
    value, pv, _ = _search(_local(board), protagonist, to_move, sorted(set(area)),
                           oracle, depth, ctx, memo, frozenset())
    return value, [(-1, -1) if m is _PASS else m for m in pv]


TENUKI = (-1, -1)
"""对外 pv 的「脱先」哨兵：真实坐标不可能为负，回放/渲染时跳过但著法权照翻。"""


def _search(board: Board, protagonist: int, to_move: int, area: list[Point],
            oracle: Oracle, depth: int, ctx: SearchContext, memo: dict,
            last_capture: frozenset = frozenset()) -> tuple[int, list[Point], Optional[tuple]]:
    """返回 (结论值, pv, anchor)。anchor 见模块 docstring 的「缓存与循环的相容性」。"""
    ctx.nodes += 1
    terminal = oracle.evaluate(board, last_capture)
    if terminal is not None:
        return terminal, [], None
    if depth <= 0:
        ctx.truncated = True
        return oracle.exhausted, [], None

    key = _state_key(board, to_move)
    hit = ctx.path_index.get(key)
    if hit is not None:
        # 同形再现：双方都无法用强手段解决这个局部
        captured = any(ctx.path_captured[hit:])
        if captured:
            ctx.ko_seen = True
            return KO, [], key
        ctx.seki_seen = True
        return SEKI, [], key

    cached = memo.get((key, depth))
    if cached is not None:
        return cached[0], cached[1], None

    ctx.path_index[key] = len(ctx.path_keys)
    ctx.path_keys.append(key)

    maximizer = (to_move == protagonist)
    best_value: Optional[int] = None
    best_line: list[Point] = []
    anchor: Optional[tuple] = None

    def better(value: int) -> bool:
        if best_value is None:
            return True
        return value > best_value if maximizer else value < best_value

    def absorb(child_anchor: Optional[tuple]) -> None:
        """上交子树的循环锚点：等于本节点就清掉（本节点的值与路径无关），
        否则保留**最外层**的那个（上交内层的会让外层节点错误入缓存）。"""
        nonlocal anchor
        if child_anchor is None or child_anchor == key:
            return
        if anchor is None or ctx.path_index.get(child_anchor, 1 << 30) < ctx.path_index.get(anchor, 1 << 30):
            anchor = child_anchor

    def consider(child: Board, child_to_move: int, move,
                 captured: Iterable[Point], child_depth: int) -> None:
        nonlocal best_value, best_line
        caps = frozenset(captured)
        ctx.path_captured.append(bool(caps))
        try:
            value, pv, child_anchor = _search(child, protagonist, child_to_move, area,
                                              oracle, child_depth, ctx, memo, caps)
        finally:
            ctx.path_captured.pop()
        absorb(child_anchor)
        if better(value):
            best_value = value
            # move=None：找劫材的虚手，盘面与着法权都不变，不占 pv 的一手；
            # move=_PASS：脱先，占一手（对外返回前会被截掉，见 solve）
            best_line = ([] if move is None else [move]) + pv

    moves = [p for p in area if board.at(p) == EMPTY and board.is_legal(to_move, p)]
    # 被劫禁着挡住 → 无论局部还有没有其它应手，都可以「到别处找劫材」。
    # 这一条必须无条件给：只在「局部完全无应手」时才给的话，被提劫的一方会被迫
    # 在自己眼位里自填而输掉劫，于是角上板六这种标准劫形被误判成净死（实测）。
    # 找劫材不是白得的先手：盘面不变、着法权仍归自己，只是把劫禁着解除。
    ko_escape = board.ko_point is not None and _ko_blocked(board, to_move, board.ko_point)
    if moves:
        for p in moves:
            try:
                nb, captured_pts = _after_move(board, to_move, p)
            except IllegalMove:
                continue                      # 落子与合法性检查之间不会变卦，兜底而已
            consider(nb, other(to_move), p, captured_pts, depth - 1)
            # 拿到极值就可以剪枝：ALIVE / DEAD 都是「建设性」结论，与路径无关，
            # 提前退出不会让缓存变得不安全（只有 KO / SEKI 才依赖循环祖先）
            if best_value == (ALIVE if maximizer else DEAD):
                break
    if ko_escape:
        if not (best_value == (ALIVE if maximizer else DEAD)):
            consider(_ko_threat(board), to_move, None, (), depth - 1)
    elif not moves:
        # 局部无处可下（全是自杀）且没有劫可打 → 脱先，让对方继续；
        # 双方都无处可下就会循环 → 双活
        consider(board, other(to_move), _PASS, (), depth - 1)

    assert best_value is not None, "搜索节点必须有至少一个后继（虚手也算）"
    if anchor is None:
        memo[(key, depth)] = (best_value, best_line)
    ctx.path_keys.pop()
    del ctx.path_index[key]
    return best_value, best_line, anchor


# ---------------------------------------------------------------------------
# 兼容入口：死活判定（旧接口，返回字符串结论）
# ---------------------------------------------------------------------------
def ld_search(board: Board, victim: int, to_move: int, area: Iterable[Point],
              depth: int = 24, _memo: Optional[dict] = None,
              flags: Optional[set] = None,
              ctx: Optional[SearchContext] = None,
              oracle: Optional[Oracle] = None) -> tuple[Verdict, list[Point]]:
    """穷举判定 victim 的死活，返回 ("alive"|"seki"|"ko"|"dead", 关键变化图)。

    area = 题目的封闭区域，**全程固定不变**（理由见模块 docstring）。

    oracle 不传就用死活判据（默认）；对杀/吃子题必须传自己的判据进来，
    否则会把「提光对方」当成「做不出两眼」来算。

    flags：传入一个 set 可收集搜索途中的特征（'ko' / 'seki' / 'truncated'）。
    'ko' 现在是**一等结论**而不是「拒题信号」；'truncated' 才是——它表示深度不够、
    结论不可信，出题方必须弃题（宁可不出，也不能出错题）。
    """
    own = ctx if ctx is not None else SearchContext()
    area_set = set(area)
    if oracle is None:
        # 被判定的只能是「与区域相邻的那块棋」，不能把全盘该色的子算进去（见 local_group）
        group = local_group(board, victim, area_set)
        oracle = life_oracle(victim, area_set, candidates=group | area_set)
    value, pv = solve(board, victim, to_move, area, oracle, depth=depth,
                      ctx=own, memo=_memo)
    if flags is not None:
        if own.ko_seen:
            flags.add("ko")
        if own.seki_seen:
            flags.add("seki")
        if own.truncated:
            flags.add("truncated")
    return VERDICT_BY_VALUE[value], pv


def verdict_text(verdict: Verdict, victim: int) -> str:
    name = "黑" if victim == BLACK else "白"
    if verdict == "alive":
        return f"{name}棋已活（两个独立眼位）"
    if verdict == "seki":
        return f"{name}棋双活（没有两眼，但对方也杀不动）"
    if verdict == "ko":
        return f"{name}棋是劫（胜负取决于劫材）"
    if verdict == "dead":
        return f"{name}棋已死（做不出两眼）"
    return ""


def replay(board: Board, to_move: int, moves: list[Point]) -> Board:
    """按「玩家手 / 对手应手」交替落子，返回新棋盘。非法手抛 IllegalMove。脱先哨兵 (-1,-1) 跳过落子但著法权照翻（L3）。"""
    b = clone(board)
    color = to_move
    for p in moves:
        if p == (-1, -1):                    # 脱先哨兵（L3）：不落子、著法权照翻
            color = other(color)
            continue
        b.play(color, p)
        color = other(color)
    return b


def replayable_prefix(board: Board, to_move: int, moves: Iterable[Point]) -> list[Point]:
    """变化图里能在**真实棋盘**上一手手摆出来的最长前缀。

    搜索把「到别处找劫材」抽象掉了（盘面不变、着法权也不变），所以涉劫的
    变化图会出现「立即回提」——在真实棋盘上那是劫禁着。截到那一手之前，
    前端画幽灵子、接口摆失败图就都不会撞上非法手（实测：吃子题里出现过）。
    结论本身不受影响：截掉的只是“劫材在哪里”这个已经被抽象掉的细节。
    脱先哨兵 (-1, -1) 不落子、著法权照翻（L3：连络题的正解线靠它表达
    「对方无处可下，自己接着连」）。
    """
    b = clone(board)
    color = to_move
    out: list[Point] = []
    for p in moves:
        if p == (-1, -1):                    # 脱先哨兵（L3）：不落子、著法权照翻
            color = other(color)
            out.append(p)
            continue
        try:
            b.play(color, p)
        except IllegalMove:
            break
        out.append(p)
        color = other(color)
    return out


def find_refutation(board: Board, victim: int, goal: str, player: int,
                    area: Iterable[Point], depth: int = 24,
                    flags: Optional[set] = None,
                    oracle: Optional[Oracle] = None) -> Optional[list[Point]]:
    """玩家走错后，找出对手能推翻目标的那一手（及其后续变化）。

    goal='live' 时对手要让 victim 死；goal='kill' 时对手（= victim）要让棋活。
    oracle 见 ld_search：非死活题必须把自己的判据传进来。
    返回对手视角的主线（第 1 手即「最佳应对」），找不到返回 None。
    """
    area_list = sorted(set(area))
    opponent = other(player)
    for p in area_list:
        if board.at(p) != EMPTY or not board.is_legal(opponent, p):
            continue
        nb = clone(board)
        try:
            nb.play(opponent, p)
        except IllegalMove:
            continue
        verdict, pv = ld_search(nb, victim, player, area_list, depth=depth,
                                flags=flags, oracle=oracle)
        value = VERDICT_VALUE[verdict]
        if worse_than(value, goal, player_is_protagonist(goal)):
            return [p] + pv
    return None


__all__ = [
    "ALIVE", "BLACK", "Board", "DEAD", "GOAL_CAPTURE", "GOAL_KILL", "GOAL_KO_KILL",
    "GOAL_KO_LIVE", "GOAL_LIVE", "GOAL_RACE", "GOAL_SEKI", "GOALS", "IllegalMove",
    "KO", "Oracle", "Point", "SEKI", "SearchContext", "VERDICT_BY_VALUE",
    "VERDICT_VALUE", "WHITE", "capture_oracle", "clone", "connect_oracle",
    "connected", "empty_regions", "enclosed_space", "eye_regions", "eyes_in_area",
    "find_refutation", "goal_achieved", "goal_text", "goal_value", "ld_search",
    "life_oracle", "local_group", "player_is_protagonist", "race_oracle", "replay",
    "replayable_prefix", "solve", "stones_of", "true_eyes_in_area", "verdict_text",
    "worse_than",
]
