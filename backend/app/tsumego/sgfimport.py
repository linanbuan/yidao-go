"""把标准死活题 SGF 导入成平台题目（接入开源/自制题集的通道）。

与 app/game/sgf.py 的 parse_sgf 不同：那个只走主分支（复盘够用），而死活题的
**答案就是变化树**——正解是主线，兄弟分支是失败图，压平就等于把答案丢了。
所以这里是一个真正的 SGF 树解析器。

约定（业界死活题 SGF 的通行写法）：
  * 根节点用 AB[]/AW[] 摆子（**只认这两种**；AE[] 清空点与 PL[] 先手方目前不实现，
    先手方由主线第一手的颜色推定，与文件里没实现的那两条保持诚实一致）；
  * 主线（根节点往下的第一条序列）= 正解；
  * 从第一手分叉出去的兄弟分支 = 失败图（分支的第一手就是错着，其后是对方最佳应对）；
  * C[] 注释保留为讲解，GN[] 当标题；
  * 主线里出现空手（`W[]`，涉劫题集常用的找劫材写法）时**整题跳过**，
    不做静默截断（截断等于存一道答案错误的题）。

导入题不带 space（封闭眼位空间），因此判定只走「变化线匹配」，不会用本地死活
搜索兜底——那套搜索只对完全被围住的局部形成立，对实战题片段不成立。
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Optional

from ..database import SessionLocal
from ..game.rules import BLACK, IllegalMove, WHITE, from_sgf, other
from ..models import TsumegoProblem
from .library import tier_of
from .store import problem_board

logger = logging.getLogger("go.tsumego.import")

GOAL_LIVE = "live"
GOAL_KILL = "kill"

_LIVE_HINTS = ("活", "live", "生", "做眼", "两眼")
_KILL_HINTS = ("杀", "死", "kill", "dead", "破眼", "攻")


# ---------------------------------------------------------------------------
# SGF 树解析
# ---------------------------------------------------------------------------
_PROP_RE = re.compile(r"([A-Z]{1,2})((?:\s*\[(?:[^\]\\]|\\.)*\])+)")
_VAL_RE = re.compile(r"\[(?:[^\]\\]|\\.)*\]")


def _unesc(v: str) -> str:
    return v.replace("\\]", "]").replace("\\[", "[").replace("\\\\", "\\")


def _parse_props(chunk: str) -> dict[str, list[str]]:
    props: dict[str, list[str]] = {}
    for m in _PROP_RE.finditer(chunk):
        props[m.group(1)] = [_unesc(v[1:-1]) for v in _VAL_RE.findall(m.group(2))]
    return props


def parse_game_trees(text: str) -> list[dict]:
    """解析 SGF 文本，返回游戏树列表；每棵树 = {"props": {...}, "children": [...]}。

    一个文件可以含多棵树（题集常见），每棵树是一道题。
    结构完全按 SGF 语义：同一棵树里的 ;A;B 串成链，( ... ) 分支挂在它出现时
    的那个节点下（所以 (;setup;B[cc](;W[x])(;W[y])) 里两个变化是 B[cc] 的兄弟）。
    """
    trees: list[dict] = []
    stack: list[dict] = []                 # 当前嵌套的树
    tails: list[Optional[dict]] = []       # 每棵树序列的尾节点（分支要挂在它下）
    buf: list[str] = []

    def flush_node() -> None:
        raw = "".join(buf).strip()
        buf.clear()
        if not raw or not stack:
            return
        node = {"props": _parse_props(raw), "children": []}
        if tails[-1] is None:
            stack[-1]["children"].append(node)     # 本树第一个节点
        else:
            tails[-1]["children"].append(node)     # 接在序列尾部
        tails[-1] = node

    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "(":
            flush_node()
            tree = {"props": {}, "children": []}
            if stack:
                (tails[-1] or stack[-1])["children"].append(tree)
            else:
                trees.append(tree)
            stack.append(tree)
            tails.append(None)
            i += 1
            continue
        if ch == ")":
            flush_node()
            if stack:
                stack.pop()
                tails.pop()
            i += 1
            continue
        if ch == ";":
            flush_node()
            i += 1
            continue
        if ch == "\\" and i + 1 < n:      # 转义：原样保留，交给属性正则处理
            buf.append(text[i:i + 2])
            i += 2
            continue
        buf.append(ch)
        i += 1
    flush_node()
    return trees


def unwrap_tree(node: dict) -> dict:
    """把 `( ... )` 包装节点展开成它的第一个真节点。

    解析器给每个 `(` 建了一个 props 为空的容器节点，而题目关心的是里面的
    落子节点；同一容器里的兄弟分支（失败图）要挂到展开后的节点上，
    否则「主线 vs 变化」的层级就丢了。
    """
    while not node["props"] and node["children"]:
        first = dict(node["children"][0])
        first["children"] = list(first["children"]) + list(node["children"][1:])
        node = first
    return node


def _kids(node: dict) -> list[dict]:
    """展开后的子节点列表（只保留带落子的）。"""
    out = []
    for c in node["children"]:
        u = unwrap_tree(c)
        if _first_move(u):
            out.append(u)
    return out


def _first_move(node: dict) -> Optional[tuple[int, str]]:
    for key, color in (("B", BLACK), ("W", WHITE)):
        if key in node["props"] and node["props"][key]:
            return color, node["props"][key][0]
    return None


def _main_sequence(node: dict) -> list[dict]:
    """沿着「第一个带落子的子节点」一路走到底，得到主线序列（不含 node 自身）。"""
    out: list[dict] = []
    cur = node
    while True:
        kids = _kids(cur)
        if not kids:
            break
        cur = kids[0]
        out.append(cur)
    return out


def _move_of(node: dict, size: int) -> Optional[tuple[int, Optional[tuple[int, int]]]]:
    fm = _first_move(node)
    if fm is None:
        return None
    color, val = fm
    val = val.strip()
    if len(val) != 2 or val == "tt":
        return color, None
    return color, from_sgf(val, size)


# ---------------------------------------------------------------------------
# 树 → 题目
# ---------------------------------------------------------------------------
def _slug(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", "-", text.lower()).strip("-")[:40]


def guess_goal(*texts: str) -> Optional[str]:
    """从标题/注释里猜目标；猜不出返回 None（调用方必须显式指定）。"""
    blob = " ".join(t for t in texts if t).lower()
    live = any(h in blob for h in _LIVE_HINTS)
    kill = any(h in blob for h in _KILL_HINTS)
    if live and not kill:
        return GOAL_LIVE
    if kill and not live:
        return GOAL_KILL
    return None


def _derive_space(size: int, setup: list, points: Iterable[list]) -> list[list]:
    """导入题的局部搜索范围：摆子与全部手顺点的**外接矩形**内的空点。

    L18：导入题此前 `space=[]`，线外落子只能含糊拒绝。现在只要局部足够小
    （≤8 个空点——对齐内置判定的面积预算 ≈165ms，越过 L7 面积墙就会指数爆炸）
    就推出空间，attempt 即可用本地搜索给真实结论。
    两点刻意为之：
      · **不外扩**：导入形的手顺点都在形状内部（做眼/紧气点在壳内），外扩的
        空点往往属于邻接战场，算进来既超预算又未必属于本题；
      · 超出预算（>8 空点）说明这张图不是封闭局部，退回旧口径 space=[]
        （仍按 SGF 手顺判定，线外落子给旧文案）。
    """
    xs: list[int] = []
    ys: list[int] = []
    for x, y, _c in setup:
        xs.append(x)
        ys.append(y)
    for x, y in points:
        xs.append(x)
        ys.append(y)
    x0 = max(0, min(xs))
    x1 = min(size - 1, max(xs))
    y0 = max(0, min(ys))
    y1 = min(size - 1, max(ys))
    occupied = {(x, y) for x, y, _c in setup}
    space = [[x, y] for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)
             if (x, y) not in occupied]
    if len(space) > 8:
        return []
    return space


def tree_to_problem(tree: dict, *, index: int, origin: str, goal: Optional[str] = None,
                    difficulty: int = 3, family: str = "导入",
                    source: str = "") -> Optional[dict]:
    """把一棵 SGF 树转成题目 dict；不合规返回 None（并记日志说明原因）。"""
    root = unwrap_tree(tree)
    props = dict(root["props"])

    try:
        size = int(props.get("SZ", ["19"])[0].split(":")[0])
    except ValueError:
        size = 19
    if size not in (9, 13, 19):
        logger.warning("跳过：SZ=%s 不受支持（仅 9/13/19）", size)
        return None

    setup: list[list[int]] = []
    for key, color in (("AB", BLACK), ("AW", WHITE)):
        for v in props.get(key, []):
            if len(v) == 2:
                p = from_sgf(v, size)
                setup.append([p[0], p[1], color])
    if not setup:
        logger.warning("跳过：没有 AB/AW 摆子，不是死活题（可能是一整盘棋谱）")
        return None

    container = root
    seq = _main_sequence(container)
    if not seq:
        logger.warning("跳过：主线没有落子，无法确定正解")
        return None

    first_move = _move_of(seq[0], size)
    if first_move is None or first_move[1] is None:
        logger.warning("跳过：主线第一手无效")
        return None
    to_move = first_move[0]

    title = (props.get("GN") or props.get("PB") or props.get("EV") or [""])[0].strip()
    title = title or f"{origin} 第 {index + 1} 题"
    comment = (props.get("C") or seq[0]["props"].get("C") or [""])[0].strip()
    resolved_goal = goal or guess_goal(title, comment, props.get("RE", [""])[0])
    if resolved_goal not in (GOAL_LIVE, GOAL_KILL):
        logger.warning("跳过 %s：无法判断目标是做活还是杀棋（用 --goal 指定）", title)
        return None

    # 主线 = 正解：逐手回放，遇到非法手就放弃这道题（宁可不导，不能导错）
    main_points: list[list[int]] = []
    every_point: list[list[int]] = []       # 全部手顺点（主线 + 失败图），用于推搜索范围
    probe = problem_board(setup, size)
    ok = True
    for node in seq:
        mv = _move_of(node, size)
        if mv is None:
            break
        if mv[1] is None:
            # 空手/找劫材（`W[]`）在涉劫题集里是常见的脱先写法。旧实现直接 break，
            # 把后续手顺**静默丢弃**，题目照常入库 —— 正解被截断成一手，
            # 与文件自己写的「宁可不导，不能导错」相反。这里整题跳过并说明原因，
            # 而不是存一道答案错误的题（审计 T2）。
            logger.warning("跳过 %s：主线含空手/脱先（涉劫找劫材写法），当前导入器不支持",
                           title)
            ok = False
            break
        try:
            probe.play(mv[0], mv[1])
        except IllegalMove as exc:
            logger.warning("跳过 %s：主线第 %d 手非法（%s）", title, len(main_points) + 1, exc)
            ok = False
            break
        main_points.append([mv[1][0], mv[1][1]])
    if not ok or not main_points:
        return None
    every_point += main_points

    lines = [{
        "moves": main_points,
        "pv": [],
        "result": "correct",
        "comment": comment or ("正解：" + " ".join(_gtp(p, size) for p in main_points)),
    }]

    # 根部的兄弟分支 = 失败图（分支第一手是错着，其后是对方的应对）
    for branch in _kids(container)[1:]:
        bseq = [branch] + _main_sequence(branch)
        pts: list[list[int]] = []
        probe2 = problem_board(setup, size)
        bad = True
        for node in bseq:
            mv = _move_of(node, size)
            if mv is None or mv[1] is None:
                break
            try:
                probe2.play(mv[0], mv[1])
            except IllegalMove:
                bad = False
                break
            pts.append([mv[1][0], mv[1][1]])
        every_point += pts
        if not bad or not pts or pts[0] == main_points[0]:
            continue
        bcomment = (branch["props"].get("C") or [""])[0].strip()
        lines.append({
            "moves": pts, "pv": [], "result": "wrong",
            "comment": bcomment or (f"{_gtp(pts[0], size)} 不成立。"),
        })

    victim = to_move if resolved_goal == GOAL_LIVE else other(to_move)
    digest = hashlib.sha1(f"{origin}|{index}|{title}|{setup}|{main_points}".encode("utf-8")).hexdigest()[:12]
    # L18：局部足够小（≤12 空点）就推出搜索范围，线外落子可用本地搜索兜底；
    # 范围太大说明不是封闭局部，space=[] 退回纯手顺判定（注释见 _derive_space）。
    space = _derive_space(size, setup, every_point)
    return {
        "pid": f"imp-{_slug(title) or 'problem'}-{digest}",
        "title": title,
        "kind": "life",          # 导入题按死活判定走（L18 起小局部带搜索范围）
        "family": family,
        "goal": resolved_goal,
        "difficulty": int(difficulty),
        # 五档标签必须一起算：只写 difficulty 的话 tier 列会落到列默认值「入门」，
        # 与难度长期不自洽（`?tier=中级` 永远查不到导入题，审计 T1）。
        "tier": tier_of(int(difficulty)),
        "size": size,
        "toMove": to_move,
        "victim": victim,
        "setup": setup,
        "space": space,
        "targets": [],
        "own": [],
        "lines": lines,
        "hint": comment[:200] if comment else "看题目的第一手方向。",
        "note": comment,
        "tags": [family, "导入"],
        "source": source or origin,
        "builtin": False,
    }


def _gtp(point, size: int) -> str:
    from ..game.rules import to_gtp
    return to_gtp((int(point[0]), int(point[1])), size)


def import_sgf_text(text: str, *, origin: str = "sgf", goal: Optional[str] = None,
                    difficulty: int = 3, family: str = "导入",
                    source: str = "") -> tuple[list[dict], list[str]]:
    """解析一段 SGF 文本（可含多棵树），返回 (题目列表, 跳过原因列表)。"""
    problems: list[dict] = []
    skipped: list[str] = []
    trees = parse_game_trees(text)
    if not trees:
        return problems, ["文件里没有可识别的 SGF 游戏树"]
    for i, tree in enumerate(trees):
        try:
            p = tree_to_problem(tree, index=i, origin=origin, goal=goal,
                                difficulty=difficulty, family=family, source=source)
        except Exception as exc:   # noqa: BLE001
            p = None
            skipped.append(f"第 {i + 1} 题解析异常：{exc}")
        if p is None:
            skipped.append(f"第 {i + 1} 题不符合死活题格式（缺摆子/主线/目标）")
        else:
            problems.append(p)
    return problems, skipped


def save_problems(problems: list[dict], replace: bool = False) -> int:
    """写库（按 id upsert）。replace=True 时覆盖已存在的同 id 题目。"""
    saved = 0
    with SessionLocal() as db:
        for data in problems:
            row = db.get(TsumegoProblem, data["pid"])
            if row is not None and not replace:
                continue
            if row is None:
                row = TsumegoProblem(id=data["pid"], builtin=False)
                db.add(row)
            row.title = data["title"]
            row.kind = data.get("kind", "life")
            row.family = data["family"]
            row.goal = data["goal"]
            row.difficulty = data["difficulty"]
            row.tier = data.get("tier") or tier_of(int(data["difficulty"]))
            row.size = data["size"]
            row.to_move = data["toMove"]
            row.victim = data["victim"]
            row.setup = data["setup"]
            row.space = data["space"]
            row.targets = data.get("targets", [])
            row.own = data.get("own", [])
            row.lines = data["lines"]
            row.hint = data["hint"]
            row.note = data["note"]
            row.tags = data["tags"]
            row.source = data["source"]
            row.builtin = False
            row.enabled = True
            saved += 1
        db.commit()
    return saved


__all__ = ["GOAL_KILL", "GOAL_LIVE", "guess_goal", "import_sgf_text",
           "parse_game_trees", "save_problems", "tree_to_problem", "unwrap_tree"]
