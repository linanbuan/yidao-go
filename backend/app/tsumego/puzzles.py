"""新题型：对杀（比气）、吃子手筋，以及枚举出来的更多劫形。

三种题型共用 library.derive_problem 的四道闸门流水线，差别只在 Spec 里的**判据与目标**：

    对杀 race    → race_oracle：谁的目标块先被提光谁输
    吃子 capture → capture_oracle：限手数内提掉标记的子
    劫形 ko      → life_oracle：结论是 KO，先走方决定它是劫活还是劫杀

## 局面怎么来的：模板枚举 + 搜索筛选，不靠人记答案

对杀与吃子的局面由模板参数化枚举（_race_spec / _capture_candidates），再用四道闸门筛。
「答案对不对」由搜索保证，「题好不好」由闸门保证，两者都不依赖人的记忆。

枚举出来的对杀结论还会与**教材的比气口诀**对照（tests/test_tsumego.py 逐条钉住）：

    无公气：气多者胜；气相同则先走者胜

公气/双活不出题，理由见 race_specs 的 docstring。

## 生成成本

枚举 + 搜索筛选很贵（全量跑一次要几分钟），所以结果由 store 层缓存到磁盘，
缓存以源码指纹失效（见 store.seed_builtin）。任何一处枚举上限的调整都要重新量一次时间。
"""
from __future__ import annotations

from typing import Iterable, Optional

from ..game.rules import BLACK, EMPTY, Point, WHITE, other, to_gtp
from .library import (GOAL_KIND, KIND_CAPTURE, KIND_CONNECT, KIND_KO, KIND_LIFE,
                      KIND_RACE, SOURCE_NOTE, Shape, Spec, board_from_setup,
                      derive_problem, make_oracle, probe_verdict)
from .solve import (GOAL_CAPTURE, GOAL_CONNECT, GOAL_CUT, GOAL_KILL, GOAL_KO_KILL,
                    GOAL_KO_LIVE, GOAL_LIVE, GOAL_RACE, GOAL_SEKI, SearchContext,
                    VERDICT_BY_VALUE, all_points, connected, enclosed_space,
                    goal_text, solve)


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def _rim(points: Iterable[Point], size: int) -> set[Point]:
    out: set[Point] = set()
    for x, y in points:
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < size and 0 <= ny < size:
                out.add((nx, ny))
    return out


def _libs(board, group: Iterable[Point]) -> set[Point]:
    """一组棋子（视为整体）的气。"""
    libs: set[Point] = set()
    for p in group:
        for n in board.neighbors(p):
            if board.at(n) == EMPTY:
                libs.add(n)
    return set(libs) - set(group)


def _connected_shapes(universe: Iterable[Point], seed: Point,
                      min_size: int, max_size: int) -> list[frozenset]:
    """universe 内包含 seed 的连通子集（每个只生成一次）。"""
    uni = set(universe)

    def neighbors(p: Point) -> list[Point]:
        x, y = p
        return [(nx, ny) for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))
                if (nx, ny) in uni]

    out: list[frozenset] = []

    def rec(cur: frozenset, frontier: list[Point]) -> None:
        if min_size <= len(cur) <= max_size:
            out.append(cur)
        if len(cur) >= max_size:
            return
        for i, p in enumerate(frontier):
            nxt = cur | {p}
            cand = set(frontier[i + 1:]) | set(neighbors(p))
            rec(nxt, sorted(cand - nxt))

    rec(frozenset({seed}), sorted(neighbors(seed)))
    return out


def _canonical_corner(shapes: Iterable[frozenset], size: int) -> list[frozenset]:
    """角部形状去对称（关于对角线翻转等价）。

    比较键用**排序元组**而不是 frozenset：集合的 `<` 是子集关系、不是全序，
    拿它做 min 会得到与枚举顺序相关的随机结果，对称形就漏掉了。
    """
    seen: set[tuple] = set()
    out: list[frozenset] = []
    for sp in shapes:
        keys = [tuple(sorted(sp)), tuple(sorted((y, x) for (x, y) in sp))]
        k = min(keys)
        if k in seen:
            continue
        seen.add(k)
        out.append(sp)
    return out


# ---------------------------------------------------------------------------
# 题型一：对杀（比气）
# ---------------------------------------------------------------------------
def _race_spec(b: int, w: int, rows: int, player: int, size: int = 9) -> Optional[Spec]:
    """边上**外气**对杀模板：黑块 b×rows、白块 w×rows，两块直接相贴（无公气）。

    以 rows=1、b=3、w=2 为例（X=黑 O=白 .=搜索范围）：

        y=2   O  O  O  X  X  X      ← 封压：黑侧用白子压、白侧用黑子压
        y=1   .  .  .  .  .  X      ← 搜索范围 = 双方的外气（右侧一颗黑子封锁）
        y=0   X  X  X  O  O  X

    两条硬规则（都是实测踩出来的）：
      1. **封压那一排不能省**。没封的时候，往自己外气里长一子就长出了搜索范围
         外的新气（对方永远填不到），于是「1 气 vs 2 气」被算成了双活。
      2. **范围里不能有中立点**。封压紧贴范围时，墙的主人可以在范围里下一手
         连回自己的墙——那一手既安全又不损己方气数，等于白得一次 pass，
         而比气就是比节奏，多一次 pass 就能把气少的一方抬成胜者（实测：
         白 1 口外气 vs 黑 2 口，白先竟然赢）。所以本模板只取 gap=0：
         范围里的**每一个点都是某一方的气**，下一手必定减少对方的气，没有白得的先手。

    预期结论（教材口诀）：气多者胜；气相同则先走者胜。
    """
    if b < 1 or w < 1 or rows < 1:
        return None
    total = b + w
    if total + 1 > size or rows + 2 > size:
        return None
    black_block = [(x, y) for x in range(b) for y in range(rows)]
    white_block = [(b + x, y) for x in range(w) for y in range(rows)]
    area = sorted((x, rows) for x in range(total))

    setup: list[tuple[int, int, int]] = ([(x, y, BLACK) for (x, y) in black_block]
                                         + [(x, y, WHITE) for (x, y) in white_block])
    # 右侧封锁（白棋不能往右长）；这些子在范围外、自带外气，不会被反吃
    for y in range(rows + 1):
        setup.append((total, y, BLACK))
    # 上方封压：黑侧用白子、白侧用黑子，谁也不能靠「往上长」凭空长气
    for x in range(b):
        setup.append((x, rows + 1, WHITE))
    for x in range(b, total + 1):
        setup.append((x, rows + 1, BLACK))

    own, rival = (black_block, white_block) if player == BLACK else (white_block, black_block)
    libs_own = b if player == BLACK else w
    libs_rival = w if player == BLACK else b
    who = "黑" if player == BLACK else "白"
    return Spec(
        pid=f"race-{b}-{w}-{rows}-{'b' if player == BLACK else 'w'}",
        title=f"边上对杀·{who}先（外气 {libs_own}:{libs_rival}，{rows} 排块）",
        kind=KIND_RACE,
        goal=GOAL_RACE,                       # 占位；实际目标由 probe_verdict 定
        size=size,
        setup=setup,
        area=area,
        player=player,
        protagonist=player,
        difficulty=1 + rows + (1 if libs_own == libs_rival else 0),
        family="边上",
        hint=(f"两块棋都逃不出去、也没有公气，纯比外气：{who} {libs_own} 口、"
              f"对方 {libs_rival} 口。"),
        note="",
        tags=["对杀", "比气"],
        own=list(own),
        targets=list(rival),
    )


RACE_NOTES = {
    "race": "紧气要从**对方的外气**紧起。公气是双方的共同命脉，先紧公气等于自杀："
            "你填一口公气，自己的气也跟着少一口，对方反而先提掉你。",
    "seki": "这就是**双活**：公气谁都不敢先填，两块棋共用一口气活下去。"
            "双活不用做两眼也算活，是围棋里唯一「没有眼也活」的情形。",
}


def _race_goal_and_note(verdict: str) -> Optional[tuple[str, str, str]]:
    """对杀结论 → (goal, 题型 kind, 讲解)。打不出来（深度不够/己方输）返回 None。"""
    if verdict == "alive":
        return GOAL_RACE, KIND_RACE, RACE_NOTES["race"]
    if verdict == "seki":
        return GOAL_SEKI, KIND_RACE, RACE_NOTES["seki"]
    return None


def race_specs(size: int = 9) -> list[dict]:
    """枚举对杀题：外气 1~3 × 1~3 × 块高 1~2 × 两个先走方（= 36 个局面）。

    每个局面的结论都与教材比气口诀对账（tests/test_tsumego.py 逐条钉住）：
    气多者胜、气同则先走者胜、气少先走也输（后者不成题，自动被闸门 2 挡掉）。

    ## 为什么不出「公气 / 双活」题

    双活要成立，公气区 R 必须满足一条很硬的几何约束：**R 的每一个空邻点也都
    是公气**（否则某一方的棋子落在 R 里就能连到 R 外、白长出气来）。推下去就是
    「R 的边界只能由对杀的这两块棋组成」，而两块棋要互相把对方围到只剩公气，
    在有限棋盘上会一路外溢、最后填满大半个盘——摆出来的图形完全不像教材里的
    双活，练不出东西。实测也印证了这点：用封压墙拼出来的公气模板，先走方
    总能靠「填公气时连到封压墙」白得先手，结论与口诀对不上。

    所以双活在本项目里是**结论**而不是题型：四值搜索会算出 SEKI，失败线的讲解
    会写「这样下只能双活，吃不掉对方」，但不会专门出「黑先做双活」的题
    （那种题的正确着手往往是「别动」，与「找出唯一正解」的出题前提冲突）。
    """
    out: list[dict] = []
    seen_pid: set[str] = set()
    specs: list[Spec] = []
    for b in range(1, 4):
        for w in range(1, 4):
            for rows in (1, 2):
                for player in (BLACK, WHITE):
                    spec = _race_spec(b, w, rows, player, size=size)
                    if spec is not None:
                        specs.append(spec)

    for spec in specs:
        verdict = probe_verdict(spec)
        if verdict is None:
            continue                          # 深度不够 → 不出
        mapped = _race_goal_and_note(verdict)
        if mapped is None:
            continue                          # 先走也输 → 不是题
        goal, kind, note = mapped
        spec.goal, spec.kind, spec.note = goal, kind, note
        spec.unique = False                   # 对杀常有多个等价紧气顺序，不强求唯一
        problem = derive_problem(spec)
        if problem is not None and problem["pid"] not in seen_pid:
            seen_pid.add(problem["pid"])
            out.append(problem)
    return out


# ---------------------------------------------------------------------------
def _race_common_spec(b: int, w: int, k: int, rows: int, player: int, cap: int,
                      size: int = 9) -> Optional[Spec]:
    """边上**公气**对杀模板：两块棋隔 k 口公气对杀，公气列在棋盘底边开口。

    以 rows=1、b=2、w=1、k=1、cap=黑 为例（X=黑 O=白 /=搜索范围 C=盖石 G=公气点）：

        y=2   O  O  X  X  X  X  X   ← 封压行：黑侧白子压、白侧黑子压、盖石上方反色压
        y=1   X  /  C  /  /  /  X   ← 外气行：/ = 黑/白外气，C = 盖石（悬空，见下）
        y=0   X  X  G  O  X  X  X   ← 黑块 / 公气 / 白块；底边 = 棋盘边界，开口封死

    盖石的由来：公气列顶上一格如果不放子，就与封压行隔着一条空缝，双方都能从缝里
    长出气来（「填公气连到封压墙白得先手」那个病，见 race_specs 的 docstring）。
    放一颗**悬空**盖石：不贴任何一块（只贴外气行与公气顶），谁填顶格公气都会连上它，
    但盖石的气全在搜索范围内 —— 连上也不出新气，战斗保持在局部。

    L5：此前的对杀只有无公气模板（gap=0），而闸门 3 会把「气多者胜」全部拒掉
    （对方先走也赢 = 无需动手），于是只留下「气同先手胜」12 题。公气局里先手方
    也不见得必胜 —— 公气数、盖石颜色决定总气差。结论究竟如何由搜索实测，
    规律逐条钉进 tests/test_tsumego.py（教材口径：先紧对方外气，公气是命脉）。
    """
    if b < 1 or w < 1 or k < 1 or rows < 1:
        return None
    total = b + k + w
    if total + 1 > size or rows + 2 > size:
        return None
    black_block = [(x, y) for x in range(b) for y in range(rows)]
    gap_cells = [(x, y) for x in range(b, b + k) for y in range(rows)]
    white_block = [(b + k + x, y) for x in range(w) for y in range(rows)]
    black_libs = [(x, rows) for x in range(b)]
    white_libs = [(x, rows) for x in range(b + k, total)]
    caps = [(x, rows) for x in range(b, b + k)]
    area = sorted(black_libs + white_libs + gap_cells)

    setup: list[tuple[int, int, int]] = (
        [(x, y, BLACK) for (x, y) in black_block]
        + [(x, y, WHITE) for (x, y) in white_block]
        + [(x, y, cap) for (x, y) in caps])
    # 右侧封锁（白棋不能往右长）；这些子在范围外、自带外气，不会被反吃
    for y in range(rows + 1):
        setup.append((total, y, BLACK))
    # 上方封压：黑侧用白子、白侧用黑子；盖石正上方用反色子，盖石连不上去
    for x in range(b):
        setup.append((x, rows + 1, WHITE))
    for x in range(b, b + k):
        setup.append((x, rows + 1, other(cap)))
    for x in range(b + k, total + 1):
        setup.append((x, rows + 1, BLACK))

    own, rival = (black_block, white_block) if player == BLACK else (white_block, black_block)
    # 标题里给「原始外气」与「公气」两个数（盖石对总气的影响由测试钉住）
    libs_own = b if player == BLACK else w
    libs_rival = w if player == BLACK else b
    who = "黑" if player == BLACK else "白"
    cap_code = "b" if cap == BLACK else "w"
    return Spec(
        pid=f"race-{b}-{w}-k{k}-{rows}r-c{cap_code}-{'b' if player == BLACK else 'w'}",
        title=f"边上对杀·{who}先（外气 {libs_own}:{libs_rival} · 公气 {k}）",
        kind=KIND_RACE,
        goal=GOAL_RACE,                       # 占位；实际目标由 probe_verdict 定
        size=size,
        setup=setup,
        area=area,
        player=player,
        protagonist=player,
        difficulty=1 + rows + k + (1 if libs_own == libs_rival else 0),
        family="边上",
        hint=(f"两块棋之间还有 {k} 口公气。公气是双方的共同命脉：{who} 要先紧**对方的外气**，"
              f"绝不能先填公气（填一口自己少一口，等于自杀）。"),
        note="",
        tags=["对杀", "公气"],
        own=list(own),
        targets=list(rival),
    )


def _classify_race_point(point, spec: Spec) -> str:
    """对杀局面里一个搜索范围点是什么：gap 公气 / rival 对方外气 / own 己方外气。

    公气点同时贴着两块棋；只贴对方块的是对方外气；只贴己方块的是己方外气。
    提示文案分流与测试都靠这个分类。
    """
    own = {tuple(q) for q in spec.own}
    rival = {tuple(q) for q in spec.targets}
    # 用列表而不用生成器：下面两个 any() 会各遍历一遍，生成器被第一个吃掉第二个就恒 False
    nb = [(point[0] + dx, point[1] + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))]
    hit_own = any(q in own for q in nb)
    hit_rival = any(q in rival for q in nb)
    if hit_own and hit_rival:
        return "gap"
    if hit_rival:
        return "rival"
    return "own"


def _decorate_common_race(problem: dict, spec: Spec) -> None:
    """按正解线第一手的类型写提示：紧对方外气 / 占公气急所 / 两者皆可。

    少气方的胜线往往要从「公气急所」开手（贴着盖石的那一口），一刀切说
    「绝对不能填公气」会把学员带到沟里 —— 提示必须跟着正解线走。
    """
    kinds = set()
    for line in problem["lines"]:
        if line["result"] == "correct" and line["moves"]:
            kinds.add(_classify_race_point(tuple(line["moves"][0]), spec))
    if kinds == {"rival"}:
        problem["hint"] = ("先紧**对方的外气**。公气是双方的命脉：先填中腹公气等于自杀"
                           "（填一口自己少一口）。")
        problem["note"] = "紧气从对方的外气开始；对方外气紧完再回头收公气，它就活不出来了。"
    elif kinds == {"gap"}:
        problem["hint"] = ("先手占住**公气急所**（贴着盖石的那一口）：这一手把双方的气"
                           "都捏在自己手里。")
        problem["note"] = ("公气是命脉，但贴着盖石的那一口本身就是急所：先手占住，"
                           "对方的棋就喘不过气来。")
    else:
        problem["hint"] = ("公气对杀：先紧**对方的外气**，或先占公气急所——"
                           "两条路都不能把中腹公气白送。")


def race_common_specs(size: int = 9) -> list[dict]:
    """枚举公气对杀：外气 1~3 × 外气 1~3 × 公气 1~2 × 块高 1~2 × 两个先走方。

    只出「盖石与先手同一方」的题（盖石 = 己方优势子）：搜索实测盖石一方的先手
    永远能胜（黑盖黑先 / 白盖白先各 36 组全活）；能不能出题由闸门 3 定 ——
    对方先走也活（盖石优势大到不需要先手）就不出，与无公气模板同一套口径。
    另外还拦一道「没有练点」：正解线首手如果覆盖所有空点（怎么下都赢），
    出给学生就是白捡——闸门 3 只管对方先走，这一道管「己方有没有可错的下法」。
    局面的结论逐条钉在 tests/test_tsumego.py（L5）：盖石方先手必活是硬规律，
    「对方先走活不活」没有一句口诀能概括（盖石位置与块高会翻转胜负），
    由逐组对账表钉住，防回归不靠印象。
    双活结论仍只当结论不当题型（闸门 3 会拦，与 race_specs 同理由）。
    """
    out: list[dict] = []
    seen_pid: set[str] = set()
    specs: list[Spec] = []
    for b in range(1, 4):
        for w in range(1, 4):
            for k in (1, 2):
                for rows in (1, 2):
                    for player in (BLACK, WHITE):
                        spec = _race_common_spec(b, w, k, rows, player, player,
                                                 size=size)   # 盖石 = 先手方
                        if spec is not None:
                            specs.append(spec)

    for spec in specs:
        verdict = probe_verdict(spec)
        if verdict is None:
            continue                          # 深度不够 → 不出
        mapped = _race_goal_and_note(verdict)
        if mapped is None:
            continue                          # 先走也输 → 不是题
        goal, kind, note = mapped
        spec.goal, spec.kind, spec.note = goal, kind, note
        spec.unique = False                   # 对杀常有多个等价紧气顺序，不强求唯一
        problem = derive_problem(spec)
        if problem is None or problem["pid"] in seen_pid:
            continue
        # 没有「错误第一手」的题 = 怎么下都赢 = 没有练点（闸门 3 只管对方先走）
        if not any(line["result"] == "wrong" for line in problem["lines"]):
            continue
        _decorate_common_race(problem, spec)
        seen_pid.add(problem["pid"])
        out.append(problem)
    return out


# ---------------------------------------------------------------------------
# 题型二：吃子手筋
# ---------------------------------------------------------------------------
def _fmt_pts(points, size: int) -> str:
    """坐标列表 → 「A1、B2」。题目标题靠它区分（否则几十道吃子题全叫一个名字）。"""
    return "、".join(to_gtp((int(x), int(y)), size) for x, y in sorted(points))


def _capture_spec(white: frozenset, kept: frozenset, size: int = 9) -> Optional[Spec]:
    """吃子手筋模板：白一小块 + 黑包围圈，白只留 kept 这几口气。

    包围圈不必是活的——搜索只在 area 内落子，圈外的子吃不到，所以它只负责
    「限定白棋的气」。白棋能不能逃、黑棋要不要弃子，全由搜索算。
    """
    board_setup = ([(x, y, WHITE) for (x, y) in sorted(white)])
    ring = _rim(white, size) - set(white)
    black_ring = ring - set(kept)
    board_setup += [(x, y, BLACK) for (x, y) in sorted(black_ring)]

    board = board_from_setup(board_setup, size)
    if _libs(board, white) != set(kept):
        return None                                   # 气算不准（白块贴到了别的东西）
    # 范围 = 白的气 + 这些气的空邻点。后者不能省：枷（门吃）这类手筋的第一手
    # 并不在对方的气上，而是在它逃跑的路径上。
    area = sorted(set(kept) | {p for p in _rim(kept, size) if board.at(p) == EMPTY})
    if len(area) > 12 or len(kept) < 2:
        return None
    who = "黑"
    label = _fmt_pts(white, size)
    # 标题必须带上「留了哪几口气」：同一块白棋留不同的气是**不同的题**，
    # 不写就有 15 对重名（实测），靠 store 去重加「（N）」后缀分不清谁是谁。
    return Spec(
        pid=f"cap-{size}-{'-'.join(f'{x}{y}' for x, y in sorted(white))}"
            f"-{'-'.join(f'{x}{y}' for x, y in sorted(kept))}",
        title=f"吃子手筋·{who}先吃白 {label}（留气 {_fmt_pts(kept, size)}）",
        kind=KIND_CAPTURE,
        goal=GOAL_CAPTURE,
        size=size,
        setup=board_setup,
        area=area,
        player=BLACK,
        protagonist=BLACK,
        difficulty=2 + (1 if len(white) >= 3 else 0) + (1 if len(kept) >= 3 else 0),
        family="手筋",
        hint=f"吃掉标记的白子（{label}）。白棋现在有 {len(kept)} 口气，直接紧气来不及。",
        note="",
        tags=["吃子", "手筋"],
        targets=sorted(white),
    )


def capture_specs(size: int = 9, window: tuple[int, int] = (4, 3),
                  limit: int = 4000) -> list[dict]:
    """枚举吃子手筋：窗口内的白棋小形状 × 留下哪几口气。

    倒扑、接不归、枷、扑这些名字都不用事先知道——只要搜索能在白先走就吃不到的
    前提下、用唯一的一手吃掉白子，它就是一道合格的手筋题。名字由 _tesuji_name 事后归类。
    """
    universe = [(x, y) for x in range(window[0]) for y in range(window[1])]
    out: list[dict] = []
    seen: set[str] = set()
    tried = 0
    # 先整体去对称再枚举：在循环里单个去重是不生效的（影子变量 + 单元素列表）
    shapes = _canonical_corner(_connected_shapes(universe, (1, 1), 2, 3), size)
    for shape in shapes:
        board_setup = [(x, y, WHITE) for (x, y) in sorted(shape)]
        board = board_from_setup(board_setup, size)
        liberties = sorted(_libs(board, shape))
        if len(liberties) < 2:
            continue
        for keep_count in (2, 3):
            if keep_count > len(liberties):
                continue
            for kept in _combinations(liberties, keep_count):
                tried += 1
                if tried > limit:
                    return out
                spec = _capture_spec(shape, set(kept), size)
                if spec is None:
                    continue
                if spec.pid in seen:
                    continue
                # 不再先用 probe_verdict 探一遍：闸门 1（玩家先走必须达成目标）
                # 与它完全等价，白跑一次根搜索会把生成时间直接翻倍（实测）。
                problem = derive_problem(spec)
                if problem is None:
                    continue                      # 吃不到 / 不唯一 / 白先也吃得到
                seen.add(spec.pid)
                _decorate_capture(problem, spec)
                out.append(problem)
    return out


def _combinations(items: list, k: int) -> list[tuple]:
    if k == 0:
        return [()]
    if k > len(items):
        return []
    out: list[tuple] = []
    for i, head in enumerate(items):
        for tail in _combinations(items[i + 1:], k - 1):
            out.append((head,) + tail)
    return out


def _decorate_capture(problem: dict, spec: Spec) -> None:
    """给吃子题补上手筋名与讲解——**从事后的变化图归类**，不靠事先记忆。

    分三类（L4 起）：
      · 正解那一手自己先被提掉 → 经典「倒扑」（首手弃子）；
      · 变化线里有「白吃掉黑子」的转换 → 「弃子提」（舍子再提的手筋结构）；
      · 其它 → 紧气吃。
    回放时合法性问题（脱先哨兵 -1,-1 与非法手）跳过，不影响出题。
    """
    correct = [l for l in problem["lines"] if l["result"] == "correct"]
    if not correct:
        return
    line = correct[0]
    first = (int(line["moves"][0][0]), int(line["moves"][0][1]))
    pv = [(int(m[0]), int(m[1])) for m in (line.get("pv") or [])]

    board = board_from_setup(spec.setup, spec.size, clear_ko_history=True)
    # 先把「留了哪几口气」算下来：下面会在这个棋盘上回放变化图，摆子就被改了。
    kept = sorted(_libs(board, spec.targets))
    color = spec.player
    sacrificed = False          # 首手弃子（经典倒扑）
    threw_away = False          # 中段弃子（白吃掉过黑子）
    try:
        board.play(color, first)
        for q in pv:
            color = other(color)
            if q == (-1, -1):                       # 脱先哨兵（L3）：翻色不落子
                continue
            caps = board.play(color, q)
            if first in {tuple(c) for c in caps}:
                sacrificed = True                   # 正解子被对方提掉了 → 弃子手筋
                break
            if color == WHITE and caps:
                threw_away = True                   # 白提过黑子 → 线里有弃子转换
    except Exception:                   # 变化图回放不下去就不标手筋名，题目本身不受影响
        pass

    label = _fmt_pts(spec.targets, spec.size)
    if sacrificed:
        name = "倒扑"
        note = ("正解这一手是**弃子**：让对方提掉它，提完之后对方的棋反而只剩一口气，"
                "于是能把更大的一块吃回来。这类手筋叫「倒扑」，要点是舍得先送一子。")
    elif threw_away:
        name = "弃子提"
        note = ("变化线里有**舍子再提**的转换：先送一到两手，白棋吃下去之后形状收紧，"
                "黑棋再一提就连本带利吃了回来。这种「让对方提、再反提」的结构"
                "是倒扑的变体（中段弃子）。")
    else:
        name = "紧气吃"
        note = ("不用弃子，靠**收紧对方的气**吃掉它。要点是先堵逃跑的方向，"
                "再从对方气最少的一侧紧起。")
    problem["title"] = f"{name}·黑先吃白 {label}（留气 {_fmt_pts(kept, spec.size)}）"
    problem["tags"] = list(problem["tags"]) + [name]
    for item in problem["lines"]:
        if item["result"] == "correct":
            item["comment"] = f"{item['comment']} {note}".strip()


def _pocket_capture_spec(white: frozenset, kept: frozenset, size: int = 9) -> Optional[Spec]:
    """口袋吃子模板（L4）：白棋 4~6 子（常带凹口 / 假眼位）+ 黑外部包围圈。

    与 `_capture_spec` 的差别：
      · 白棋形状允许有**内部空点**（看起来能成眼、能抵抗），黑环只封外气——
        旧模板的白棋全是实心小块，「黑无处可扑」正是 76 题全是紧气吃的根因
        （§9.9-1 的三条实测原因之前两条）；
      · 搜索范围 = 外气 + 外气的空邻点 + **白块的全部空邻点**——半开口的
        「扑点 / 缺口」都在里面。实测结论：完全被白围住的口袋（真眼位）
        落子即自杀、搜索用不上；白块轮廓上的凹点才是弃子手筋真正落子的地方。
    """
    board_setup = [(x, y, WHITE) for (x, y) in sorted(white)]
    ring = _rim(white, size) - set(white)
    board_setup += [(x, y, BLACK) for (x, y) in sorted(ring - set(kept))]
    board = board_from_setup(board_setup, size)
    if _libs(board, white) != set(kept):
        return None                                   # 气算不准（白块贴到了别的东西）
    area = set(kept) | {p for p in _rim(kept, size) if board.at(p) == EMPTY}
    for x, y in white:
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            q = (x + dx, y + dy)
            if 0 <= q[0] < size and 0 <= q[1] < size and board.at(q) == EMPTY:
                area.add(q)
    area = sorted(area)
    if len(area) > 12 or len(area) < 3 or len(kept) < 2:
        return None
    label = _fmt_pts(white, size)
    return Spec(
        pid=f"pcap-{'-'.join(f'{x}{y}' for x, y in sorted(white))}"
            f"-{'-'.join(f'{x}{y}' for x, y in sorted(kept))}",
        title=f"吃子手筋·黑先吃白 {label}（留气 {_fmt_pts(kept, size)}）",
        kind=KIND_CAPTURE,
        goal=GOAL_CAPTURE,
        size=size,
        setup=board_setup,
        area=area,
        player=BLACK,
        protagonist=BLACK,
        difficulty=2 + (1 if len(white) >= 5 else 0) + (1 if len(kept) >= 3 else 0),
        family="手筋",
        hint=f"吃掉标记的白子（{label}）。白棋有 {len(kept)} 口外气，还带着一点眼位的余地。",
        note="",
        tags=["吃子", "手筋"],
        targets=sorted(white),
    )


def pocket_capture_specs(size: int = 9, limit: int = 1200) -> list[dict]:
    """口袋吃子手筋（L4）：3×3 角窗内白棋 4~6 子的连通形 × 留气 2~3。

    吃子题从此不再是 100% 紧气吃：这些形状白棋有「看起来能做眼」的抵抗，
    变化线里出现真·弃子转换（归类见 `_decorate_capture`，名字从事后变化图定）。
    枚举上限（limit）是硬约束：放宽会重蹈 §8.8 / §9.9-1 记过的 >10 分钟覆辙
    （窗口再大、白棋再多子，搜索分支按面积指数涨）。
    首手弃子的经典「倒扑」在此角窗几何下没有出现（3×3 窗口内白块的凹点要么
    全白邻=自杀、要么存在更简单的紧气胜线），如实登记为开放项（§28.2）。
    """
    universe = [(x, y) for x in range(3) for y in range(3)]
    shapes = _canonical_corner(_connected_shapes(universe, (1, 1), 4, 6), size)
    out: list[dict] = []
    seen: set[str] = set()
    tried = 0
    for shape in shapes:
        board = board_from_setup([(x, y, WHITE) for (x, y) in sorted(shape)], size)
        libs = sorted(_libs(board, shape))
        if len(libs) < 2:
            continue
        for keep_count in (2, 3):
            if keep_count > len(libs):
                continue
            for kept in _combinations(libs, keep_count):
                tried += 1
                if tried > limit:
                    return out
                spec = _pocket_capture_spec(shape, set(kept), size)
                if spec is None or spec.pid in seen:
                    continue
                problem = derive_problem(spec)
                if problem is None:
                    continue
                seen.add(spec.pid)
                _decorate_capture(problem, spec)
                out.append(problem)
    return out


# ---------------------------------------------------------------------------
# 题型三：角部眼位形枚举（全部结论）+ 外气变体
# ---------------------------------------------------------------------------
# 「守先结论, 攻先结论」→ 该出哪些题（bool = 玩家是不是被围的那一方）。
# 守先的值必定 ≥ 攻先（自己先走不会更差），所以只有下三角的十种组合。
# 其余组合不出题的理由：
#   (alive, alive) 无条件活形，对方先走也杀不掉，闸门 3 会拒；
#   (dead,  dead)  已经死透，自己先走也活不了，闸门 2 会拒；
#   (*,     seki)  攻先只能做成双活——双活不做题型（几何约束见 race_specs）；
#   (seki,  *)     守先只能双活，同上。
_VERDICT_TO_GOALS: dict[tuple[str, str], tuple[tuple[str, bool], ...]] = {
    ("alive", "dead"): ((GOAL_LIVE, True), (GOAL_KILL, False)),
    ("alive", "ko"): ((GOAL_KO_KILL, False),),
    ("ko", "ko"): ((GOAL_KO_LIVE, True), (GOAL_KO_KILL, False)),
    ("ko", "dead"): ((GOAL_KO_LIVE, True),),
}

# 角上板六的外气口诀（多个独立教材源一致）：
#   「六目角，真稀奇；没有外气是死棋，一口外气是打劫，两口外气是活棋。」
# 实测守先均为 alive，攻先依次是 ko / ko / alive——后两档与口诀完全吻合。
# 0 外气那档局部结论是 ko、口诀说「死棋」，差的是一层规则：角上找不到劫材，
# 「劫尽棋亡」判死。这跟盘角曲四是同一个特例，所以 note 里必须讲清两层结论。
#
# 第三档（2 外气）**当前没有任何题目会用到它**：攻先即 alive 属无条件活形，
# （alive, alive）不在 _VERDICT_TO_GOALS 里，出题闸门也会以「无需动手」拒掉（不为此放宽闸门）。
# 留着是因为口诀三档要完整，而且它是 test_outside_liberties_match_textbook_mnemonic 对账的教材原文。
SIX_LIBERTY_NOTE = {
    0: ("角上板六无外气：局部是劫，但守方在角上找不到劫材，规则上「劫尽棋亡」判死。"
        "口诀说的「没外气是死棋」指的是这个实际结果，而局部手段仍是打劫。"),
    1: ("一口外气的角上板六仍是劫，但是**缓一气劫**：攻方要先提劫、再紧外气、"
        "最后消劫，共三步才能吃净，而普通劫只需两步。多出来的那口气就是这一口外气。"),
    2: ("两口及以上外气的角上板六是**净活**：攻方点入后，守方靠「胀牯牛」就能做出两眼。"
        "同一个形状，差别全在外气——这就是为什么数外气是角上死活的第一步。"),
}


def corner_shape_specs(size: int = 9, window: int = 4, min_size: int = 3,
                      max_size: int = 7, limit: int = 400) -> list[dict]:
    """枚举角部眼位空间，把**所有能成题的结论**都拿出来出题。

    旧版本（ko_shape_specs）只留结论是「劫」的形状，等于把 187 个规范形里的大部分
    白白丢掉。现在先用一对便宜的探测拿到（守先, 攻先）两个结论，再按
    _VERDICT_TO_GOALS 反推该出哪些题——只有过得了预探的形状才会进
    derive_problem（它自己还要再跑 2 + |area| 次搜索，是贵的那一步）。
    """
    from .library import SHAPES, build_setup, validate_setup

    known = {frozenset(s.space) for s in SHAPES}
    universe = [(x, y) for x in range(window) for y in range(window)]
    out: list[dict] = []
    tried = 0
    for shape_pts in _canonical_corner(_connected_shapes(universe, (0, 0), min_size, max_size), size):
        tried += 1
        if tried > limit:
            break
        if shape_pts in known:
            continue
        base = Shape(key="corner-probe", title="corner-probe", space=sorted(shape_pts),
                     difficulty=4, family="角上", hint="", note="", size=size, victim=BLACK)
        out += _specs_from_shape(base, known)
    return out


def _specs_from_shape(base: Shape, known=None) -> list[dict]:
    """一个眼位形（可带外气）→ 它对应的全部题目。摆子不成立或没有可出的结论就返回空。"""
    from .library import build_setup, validate_setup

    known = known or set()
    try:
        setup, board = build_setup(base)
        validate_setup(base, board)
    except ValueError:
        return []                                  # 包围圈自动生成不出来 / 气超出了范围
    area = sorted(set(enclosed_space(board, base.victim)) | set(base.outside_libs))
    if not area or len(area) > 8 or frozenset(area) in known:
        # area 上限 8 是实测硬墙：life 判据在 area=9、depth=12 就要 1.3s 且截断，
        # area=12 要 15.6s，area=16 超过 90s 跑不完（而出题用的是 DEPTH=28）。
        return []

    probe = Spec(pid="probe", title="probe", kind=KIND_LIFE, goal=GOAL_LIVE, size=base.size,
                 setup=setup, area=area, player=base.victim, protagonist=base.victim,
                 difficulty=4, family=base.family, hint="", note="")
    defend = probe_verdict(probe)                    # 守先（被围的一方先走）
    if defend is None:
        return []
    probe.player = other(base.victim)
    attack = probe_verdict(probe)                    # 攻先
    if attack is None:
        return []

    out: list[dict] = []
    for goal, player_is_victim in _VERDICT_TO_GOALS.get((defend, attack), ()):
        player = base.victim if player_is_victim else other(base.victim)
        spec = _corner_spec(base, setup, area, goal, player, defend, attack)
        problem = derive_problem(spec)
        if problem is not None:
            out.append(problem)
    return out


def _corner_spec(shape: Shape, setup, area: list[Point], goal: str, player: int,
                 defend: str, attack: str) -> Spec:
    """把角部枚举形包成题目规格。难度交给 grade_difficulty 自动打分（不再手填）。"""
    libs = len(shape.outside_libs)
    # 形状名：外气变体知道自己是哪个基本形（角上板六/盘角曲四…），就用它的名字；
    # 枚举出来的形状没有名字，只能报目数。只写「角上6目」会把板六/曲六/葡萄六
    # 全撞到同一个标题上，靠 store 去重加「（N）」后缀根本分不清谁是谁。
    base_name = shape.title if shape.title and shape.title != "corner-probe" \
        else f"角上{len(shape.space)}目"
    label = base_name
    if libs:
        # 外气在哪个点必须写进标题：同一个眼位在不同位置开口气是**不同的题**。
        label += "·外气" + "".join(to_gtp(p, shape.size) for p in sorted(shape.outside_libs))
    who = "黑" if player == BLACK else "白"
    text = goal_text(goal)
    note = shape.note or (
        "这道题的形状是枚举角部眼位得到的，结论由穷举搜索算出（不是手抄答案）。"
        "角上因为少了一口外气，很多在边上净活的形状到了角上只能打劫。"
    )
    return Spec(
        pid=f"corner-{'-'.join(f'{x}{y}' for x, y in sorted(shape.space))}"
            f"-L{libs}-{'-'.join(f'{x}{y}' for x, y in sorted(shape.outside_libs))}-{goal}",
        title=f"{label}·{who}先{text}",
        kind=GOAL_KIND[goal],
        goal=goal,
        size=shape.size,
        setup=setup,
        area=area,
        player=player,
        protagonist=shape.victim,
        difficulty=4,                     # 占位；auto_difficulty=True 会重算
        family="角上",
        hint=(f"{base_name}的形状"
              + (f"，外围有 {libs} 口外气" if libs else "，没有外气")
              + f"。{who}先走，正解只有一处。"),
        note=note,
        tags=["角部", text] + ([f"{libs}口外气"] if libs else []),
    )


def liberty_variant_specs(size: int = 9, max_points: int = 3,
                          max_libs: int = 2) -> list[dict]:
    """给角部基本形加 1~2 口外气，生成同形变体。

    外气是教材里结论的分水岭，而旧家族因为 validate_setup 要求「气全落在眼位内」，
    所有题都是零外气。外气点由 _liberty_points 自动从封锁圈里找，不靠人手摆。
    """
    from .library import SHAPES

    known = {frozenset(s.space) for s in SHAPES}
    out: list[dict] = []
    seen_setup: set[frozenset] = set()
    for shape in SHAPES:
        if shape.family != "角上":
            continue
        cands = _liberty_points(shape)[:max_points]
        for n in range(1, max_libs + 1):
            for libs in _combinations(cands, n):
                variant = Shape(
                    key=f"{shape.key}-L{n}", title=shape.title, space=list(shape.space),
                    difficulty=shape.difficulty, family=shape.family, hint="", note="",
                    size=size, victim=shape.victim, outside_libs=list(libs),
                )
                if len(set(shape.space)) + n > 8:
                    continue                     # area 硬上限
                problems = _specs_from_shape(variant, known)
                for p in problems:
                    # 按摆子内容去重：不同外气点可能产生对称等价的同一个局面，
                    # 标题不同但内容一样，不去重就会把同一道题当两道发下去。
                    key = frozenset((x, y, c) for x, y, c in p["setup"])
                    if key in seen_setup:
                        continue
                    seen_setup.add(key)
                    _annotate_liberty(p, shape, n)
                    out.append(p)
    return out


def _liberty_points(shape: Shape) -> list[Point]:
    """从攻方封锁圈里找出「拿掉就能当外气」的点（= 贴着受害方墙的攻方子）。"""
    from .library import build_setup

    _, board = build_setup(shape)
    victim = {p for p in all_points(board) if board.at(p) == shape.victim}
    foe = other(shape.victim)
    attacker = {p for p in all_points(board) if board.at(p) == foe}
    return sorted(p for p in attacker if _rim({p}, shape.size) & victim)


def _annotate_liberty(problem: dict, base: Shape, libs: int) -> None:
    """给外气变体补上口诀讲解——角上板六三档的结论有教材口诀可对账，其余形状只说外气的作用。"""
    if base.key == "corner-six":
        note = SIX_LIBERTY_NOTE.get(libs, "")
    else:
        note = (f"同一个形状加上 {libs} 口外气：外气多一口，对杀与做眼的余地就大一分，"
                "很多角上只能打劫的形状因此变成净活。数外气是角上死活的第一步。")
    if not note:
        return
    problem["note"] = note
    for line in problem["lines"]:
        if line["result"] == "correct":
            line["comment"] = f"{line['comment']} {note}".strip()


def ko_shape_specs(size: int = 9, window: int = 4, min_size: int = 4,
                   max_size: int = 7, limit: int = 400) -> list[dict]:
    """角部枚举里结论是「劫」的那一部分。保留这个入口是为了语义清楚：
    corner_shape_specs 已经把它包含了，这里只是把劫形挑出来给测试与统计用。"""
    return [p for p in corner_shape_specs(size=size, window=window, min_size=min_size,
                                         max_size=max_size, limit=limit)
            if p["kind"] == KIND_KO]


# ---------------------------------------------------------------------------
# 题型四：连络 / 切断
# ---------------------------------------------------------------------------
def _connect_spec(w: int, goal: str, size: int = 9) -> Optional[Spec]:
    """连络模板：对方 w 颗子卡在二路，两边的己方棋只能从**一线缺口**连过去。

    以 w=2 为例（X=己方 O=对方 .=搜索范围，坐标从角上算起）：

        y=2   .  X  X  .
        y=1   X  O  O  X
        y=0   X  .  .  X      ← 缺口：唯一能连过去的两个点

    为什么这个形能成题（手工推演过，搜索也认）：对方来占缺口时，那一子虽然能连回
    自己二路的棋，但连完以后整块只剩一口气（两边都被己方卡死），下一手就被提——
    这就是**接不归**。缺口窄一格（w=1）时对方连下手的余地都没有（填进去就是零口气
    的自杀手），于是「对方先走也拦不住」，闸门 3 会把那种题拒掉；w=3 时己方两手连不完。
    所以只有 w=2 能成题，这是模板的性质、不是搜索漏了。

    两个端点存在 own 里（make_oracle 的 KIND_CONNECT 分支就认这两个点），
    而不是 targets：targets 留给「要吃掉的目标子」，语义不同。
    """
    if w < 1 or w + 3 > size:
        return None
    foe = [(x, 1) for x in range(1, w + 1)]              # 对方卡二路的那几颗
    gap = [(x, 0) for x in range(1, w + 1)]               # 一线缺口 = 搜索范围
    ring = _rim(foe, size) - set(foe) - set(gap)          # 围住它们的己方墙
    ends = [(0, 0), (w + 1, 0)]                           # 要连上的两块棋的代表点
    mine = sorted(ring | set(ends))

    player = BLACK if goal == GOAL_CONNECT else WHITE
    setup = ([(x, y, BLACK) for (x, y) in mine]
             + [(x, y, WHITE) for (x, y) in foe])
    board = board_from_setup(setup, size)
    # 端点必须真是己方的子、而且开局确实**没连上**（否则这题不成立）
    for e in ends:
        if board.at(e) != BLACK:
            return None
    if connected(board, BLACK, ends[0], ends[1]):
        return None
    who = "黑" if player == BLACK else "白"
    text = goal_text(goal)
    return Spec(
        pid=f"connect-{w}-{goal}",
        title=f"一线连络·{who}先{text}（{w} 子缺口）",
        kind=KIND_CONNECT,
        goal=goal,
        size=size,
        setup=setup,
        area=sorted(gap),
        player=player,
        protagonist=BLACK,                    # 被判定的永远是「想连的那一方」
        difficulty=4,                         # 占位；auto_difficulty 会重算
        family="边上",
        hint=(f"{who}先走。两块棋被对方卡在二路的 {w} 颗子隔开，"
              f"只能从一线的缺口连过去。" if goal == GOAL_CONNECT else
              f"{who}先走。占住一线的缺口，让两块黑棋永远连不上。"),
        note=("要点在**一线**：对方虽然能占住缺口连回自己二路的棋，但连完以后"
              "整块只剩一口气，下一手就被提光——这叫「接不归」。所以缺口其实是"
              "先手一方的，这类「看着能断、实际断不掉」的形状靠的就是对方的气紧。"),
        tags=["连络", text],
        own=[list(e) for e in ends],
        targets=[list(p) for p in foe],
        unique=False,                         # 从哪一头先连都成，不强求唯一
    )


def connect_specs(size: int = 9, max_w: int = 3) -> list[dict]:
    """枚举缺口宽度 1~max_w，连络与切断各出一道。

    连络的判据与基础设施（connect_oracle / GOAL_CONNECT / KIND_CONNECT / verdict_line）
    早已就绪；**L3 起接进 build_puzzle_library**（此前不接的完整考证见 git 历史
    「为什么没有接进来」旧版 docstring，要点：w=1 白得 pass、w=2 的连通发生在对方
    脱先之后、w=3 连不完）。

    交付的钥匙是 pv 的**脱先哨兵**（solve.TENUKI = (-1,-1)，L3）：w=2 的正解线
    「己方占 B1 → 对方无处可下脱先 → 己方占 B2」里的脱先不再被截断，变化图能
    把「两块棋连上了」演示完；回放与前端渲染跳过哨兵、著法权照翻。
    跟 26 轮教训同源：**不要用「截断」回避表达不出来的步骤**，把表达补上。

    三条宽度里真的能成题的只有 w=2（模板性质，见 _connect_spec 的 docstring）；
    w=1 与 w=3 被闸门正确拒掉，不代表模板失败。
    """
    out: list[dict] = []
    for w in range(1, max_w + 1):
        for goal in (GOAL_CONNECT, GOAL_CUT):
            spec = _connect_spec(w, goal, size=size)
            if spec is None:
                continue
            problem = derive_problem(spec)
            if problem is None:
                continue
            # 交出去的正解线必须能把目标演示完：变化图为空就说明真正成立的那一手
            # 丢了（L3 前是脱先被截断），学员看完仍不知道两块棋是怎么连上的。
            if not any(line.get("pv") for line in problem["lines"]
                       if line["result"] == "correct"):
                continue
            out.append(problem)
    return out


# ---------------------------------------------------------------------------
def build_puzzle_library() -> list[dict]:
    """全部新题型题目。生成耗时受枚举上限约束（见各函数的 limit）。

    角部枚举与外气变体都走 _specs_from_shape，两者可能命中同一个形状（变体的
    area 与某个枚举形的 space 相同时），pid 不同但内容重复——store 层按标题去重。
    """
    out: list[dict] = []
    out += race_specs()
    out += race_common_specs()
    out += capture_specs()
    out += corner_shape_specs()
    out += liberty_variant_specs()
    out += connect_specs()        # L3：pv 脱先哨兵落地后接回流水线
    out += pocket_capture_specs()  # L4：白棋带内部空点、变化线含弃子转换的吃子题
    return out


__all__ = ["build_puzzle_library", "capture_specs", "connect_specs",
           "corner_shape_specs", "ko_shape_specs", "liberty_variant_specs",
           "pocket_capture_specs", "race_common_specs", "race_specs"]
