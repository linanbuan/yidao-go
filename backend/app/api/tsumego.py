"""死活题 API：题目列表、出题、判定、答案、练习统计。

判定放在服务端有两个理由：
  1. 答案（lines）不能下发给前端，否则打开开发者工具就能看；
  2. 提子、劫、自杀这些规则细节由规则引擎统一处理，前端不必重算，
     接口直接把判定后的棋盘回传，前端照着画就行。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..game.rules import IllegalMove, color_name, other, to_gtp
from ..models import TsumegoProblem, TsumegoProgress, User
from ..tsumego.library import (DEPTH, KIND_TEXT, TIERS, make_oracle, verdict_line)
from ..tsumego.solve import (find_refutation, goal_achieved, goal_text, ld_search,
                             replayable_prefix)
from ..tsumego.store import problem_board, row_spec
from .deps import get_current_user

router = APIRouter(prefix="/api/tsumego", tags=["tsumego"])

STATUS_SOLVED = "solved"
STATUS_FAILED = "failed"
STATUS_CONTINUE = "continue"

# 可筛选的目标（与 solve.GOALS 一致）。写成常量而不是正则字面量，
# 新增目标时改一处就够，也不会出现「后端支持、筛选参数不认」的两张皮。
GOAL_PATTERN = "^(live|kill|ko_live|ko_kill|seki|race|capture|connect|cut)$"
KIND_PATTERN = "^(life|ko|seki|race|capture|connect)$"
# 难度档位（library.TIERS）。筛选按档而不按 1~9 的数字：学员想的是「做中级的」，
# 不是「做难度 5 的」；difficulty 参数保留，两者可以同时用。
TIER_PATTERN = "^(入门|初级|中级|高级|段位)$"
TIER_ORDER = [name for _, name in TIERS]


# ---------------------------------------------------------------------------
class AttemptIn(BaseModel):
    """玩家到目前为止落下的每一手（内部坐标）。"""
    # 上限防止超大请求体（uvicorn 默认不限制 body 大小）
    moves: list[list[int]] = Field(default_factory=list, min_length=1, max_length=100)


def _norm(moves) -> list[tuple[int, int]]:
    return [(int(m[0]), int(m[1])) for m in moves]


def _player_moves(line: dict) -> list[tuple[int, int]]:
    """变化线里属于玩家的手（line.moves 是玩家/对手交替，玩家在下标 0、2、4…）。"""
    return _norm(line["moves"][0::2])


def _match(lines: list[dict], result: str, played: list[tuple[int, int]]) -> Optional[dict]:
    for line in lines:
        if line.get("result") != result:
            continue
        pm = _player_moves(line)
        if len(pm) >= len(played) and pm[:len(played)] == played:
            return line
    return None


def _progress(db: Session, user: User, problem_id: str) -> TsumegoProgress:
    row = db.scalar(select(TsumegoProgress).where(
        TsumegoProgress.user_id == user.id,
        TsumegoProgress.problem_id == problem_id))
    if row is None:
        # 列默认值要到 flush 才生效，而下面马上要 attempts += 1：
        # 不在这里写初值就会读到 None 而报 TypeError
        row = TsumegoProgress(user_id=user.id, problem_id=problem_id,
                              attempts=0, solved=False, seen_answer=False)
        db.add(row)
    return row


def _brief(rec: TsumegoProblem, prog: Optional[TsumegoProgress]) -> dict:
    """题目摘要：**不含 lines**（答案）。"""
    return {
        "id": rec.id,
        "title": rec.title,
        "kind": rec.kind or "life",
        "kindText": KIND_TEXT.get(rec.kind or "life", rec.kind or "life"),
        "family": rec.family,
        "goal": rec.goal,
        "goalText": goal_text(rec.goal),
        "difficulty": rec.difficulty,
        "tier": rec.tier or "入门",
        "size": rec.size,
        "toMove": rec.to_move,
        "toMoveText": color_name(rec.to_move),
        "victim": rec.victim,
        "victimText": color_name(rec.victim),
        "setup": rec.setup,
        "space": rec.space,
        "targets": rec.targets or [],
        "own": rec.own or [],
        "hint": rec.hint,
        "tags": rec.tags or [],
        "source": rec.source,
        "builtin": rec.builtin,
        "attempts": prog.attempts if prog else 0,
        "solved": bool(prog.solved) if prog else False,
        "seenAnswer": bool(prog.seen_answer) if prog else False,
    }


# ---------------------------------------------------------------------------
@router.get("/problems")
def list_problems(goal: Optional[str] = Query(default=None, pattern=GOAL_PATTERN),
                  kind: Optional[str] = Query(default=None, pattern=KIND_PATTERN),
                  tier: Optional[str] = Query(default=None, pattern=TIER_PATTERN),
                  difficulty: Optional[int] = Query(default=None, ge=1, le=9),
                  family: Optional[str] = None,
                  unsolved: bool = False,
                  wrong_only: bool = Query(default=False, alias="wrongOnly"),
                  user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    rows = db.scalars(select(TsumegoProblem).where(TsumegoProblem.enabled.is_(True))
                      .order_by(TsumegoProblem.difficulty, TsumegoProblem.id)).all()
    prog = {p.problem_id: p for p in db.scalars(
        select(TsumegoProgress).where(TsumegoProgress.user_id == user.id)).all()}
    items = []
    for rec in rows:
        if goal and rec.goal != goal:
            continue
        if kind and (rec.kind or "life") != kind:
            continue
        if tier and (rec.tier or "入门") != tier:
            continue
        if difficulty and rec.difficulty != difficulty:
            continue
        if family and rec.family != family:
            continue
        p = prog.get(rec.id)
        if unsolved and p is not None and p.solved:
            continue
        if wrong_only and not (p is not None and p.attempts > 0 and not p.solved):
            continue
        items.append(_brief(rec, p))
    return {"items": items, "total": len(items)}


@router.get("/summary")
def summary(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    total = db.scalar(select(func.count(TsumegoProblem.id))
                      .where(TsumegoProblem.enabled.is_(True))) or 0
    rows = db.scalars(select(TsumegoProgress).where(
        TsumegoProgress.user_id == user.id)).all()
    solved = sum(1 for r in rows if r.solved)
    attempts = sum(r.attempts for r in rows)
    kinds: dict[str, str] = {}
    tiers: dict[str, str] = {}
    by_goal: dict[str, dict[str, int]] = {}
    by_kind: dict[str, dict] = {}
    by_tier: dict[str, dict] = {}
    for rec in db.scalars(select(TsumegoProblem).where(
            TsumegoProblem.enabled.is_(True))).all():
        slot = by_goal.setdefault(rec.goal, {"total": 0, "solved": 0})
        slot["total"] += 1
        kind = rec.kind or "life"
        kinds[rec.id] = kind
        kslot = by_kind.setdefault(kind, {"text": KIND_TEXT.get(kind, kind),
                                          "total": 0, "solved": 0})
        kslot["total"] += 1
        tier = rec.tier or "入门"
        tiers[rec.id] = tier
        tslot = by_tier.setdefault(tier, {"total": 0, "solved": 0})
        tslot["total"] += 1
    for r in rows:
        if not r.solved:
            continue
        rec = db.get(TsumegoProblem, r.problem_id)
        if rec and rec.goal in by_goal:
            by_goal[rec.goal]["solved"] += 1
        if rec and rec.id in kinds:
            by_kind[kinds[rec.id]]["solved"] += 1
        if rec and rec.id in tiers:
            by_tier[tiers[rec.id]]["solved"] += 1
    return {
        "total": total,
        "solved": solved,
        "attempts": attempts,
        # 答对率（0~1）。与对局复盘的「平均每手损失目数」是两码事，
        # 所以不叫 accuracy，免得跟那套 0~19 目的口径混起来（§3.16）。
        "solveRate": round(solved / attempts, 3) if attempts else None,
        "byGoal": by_goal,
        "byKind": by_kind,
        # 按 library.TIERS 的固定顺序输出（而不是字典插入序），前端直接渲染不用再排
        "byTier": {name: by_tier[name] for name in TIER_ORDER if name in by_tier},
    }


@router.get("/next")
def next_problem(difficulty: Optional[int] = Query(default=None, ge=1, le=9),
                 goal: Optional[str] = Query(default=None, pattern=GOAL_PATTERN),
                 kind: Optional[str] = Query(default=None, pattern=KIND_PATTERN),
                 tier: Optional[str] = Query(default=None, pattern=TIER_PATTERN),
                 wrong_only: bool = Query(default=False, alias="wrongOnly"),
                 user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    """出题：优先没做过的，其次做错过的，最后才是已解出的（复习）。

    wrong_only=True 时只从错题本出（做过且没解出）；错题本空了给 404，
    客户端据此提示「错题重练已清零」。
    """
    rows = db.scalars(select(TsumegoProblem).where(TsumegoProblem.enabled.is_(True))
                      .order_by(TsumegoProblem.difficulty, TsumegoProblem.id)).all()
    if goal:
        rows = [r for r in rows if r.goal == goal]
    if kind:
        rows = [r for r in rows if (r.kind or "life") == kind]
    if tier:
        rows = [r for r in rows if (r.tier or "入门") == tier]
    if difficulty:
        rows = [r for r in rows if r.difficulty == difficulty]
    if not rows:
        raise HTTPException(404, "没有符合条件的题目")
    prog = {p.problem_id: p for p in db.scalars(
        select(TsumegoProgress).where(TsumegoProgress.user_id == user.id)).all()}
    if wrong_only:
        rows = [r for r in rows
                if (lambda q: q is not None and q.attempts > 0 and not q.solved)(prog.get(r.id))]
        if not rows:
            raise HTTPException(404, "错题本里没有题目")

    def rank(rec: TsumegoProblem) -> tuple:
        p = prog.get(rec.id)
        if p is None or p.attempts == 0:
            return (0, rec.difficulty, rec.id)      # 没做过
        if not p.solved:
            return (1, rec.difficulty, rec.id)      # 做过但没解出
        return (2, rec.difficulty, rec.id)          # 已解出（复习）

    rec = min(rows, key=rank)
    return {"problem": _brief(rec, prog.get(rec.id))}


@router.get("/{problem_id}")
def get_problem(problem_id: str, user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    rec = db.get(TsumegoProblem, problem_id)
    if rec is None or not rec.enabled:
        raise HTTPException(404, "题目不存在")
    prog = db.scalar(select(TsumegoProgress).where(
        TsumegoProgress.user_id == user.id,
        TsumegoProgress.problem_id == problem_id))
    return {"problem": _brief(rec, prog)}


@router.post("/{problem_id}/attempt")
def attempt(problem_id: str, body: AttemptIn, user: User = Depends(get_current_user),
            db: Session = Depends(get_db)):
    """判定玩家的一手（或一串手）。返回判定后的棋盘，前端直接照着画。

    **这里必须是 `def` 而不是 `async def`**：玩家走变化线之外的一手时，要跑最多
    三次 DEPTH=28 的局部穷举搜索（find_refutation + 两次 ld_search），是纯 CPU 活。
    FastAPI 对 `async def` 路由是**直接在事件循环里调用**的，只有 `def` 路由才丢线程池；
    写成 async def 的话，搜索期间**整个服务都卡住**——别人的对局 WebSocket 消息、
    任何接口请求全部排队。函数体里没有任何 await，所以改成 def 是等价的。
    （test_api.py 里有一条 AST 测试钉住这个规则，别再改回去。）
    """
    rec = db.get(TsumegoProblem, problem_id)
    if rec is None or not rec.enabled:
        raise HTTPException(404, "题目不存在")

    played = _norm(body.moves)
    board = problem_board(rec.setup, rec.size)
    area = _norm(rec.space)
    player, victim = rec.to_move, rec.victim
    goal = rec.goal
    size = rec.size
    lines = rec.lines or []

    # 先把「玩家手 + 已确认的对手应手」回放出来，再判定最新一手
    replies: list[tuple[int, int]] = []
    for i in range(len(played) - 1):
        line = _match(lines, "correct", played[:i + 1])
        if line is None:
            raise HTTPException(400, "前序手顺不成立，请重新做题")
        # 守卫必须看**整条 moves**的长度，而不是只看玩家手数：一条 `moves` 只有
        # 一手的线，`_player_moves` 长度是 1，`<= i` 判不出越界，随后取
        # `moves[2*i+1]` 直接 IndexError → 500（审计 T3）。
        if len(line["moves"]) <= 2 * i + 1:
            raise HTTPException(400, "这道题已经下完了，请换一题")
        replies.append(tuple(int(v) for v in line["moves"][2 * i + 1]))
    for i, pt in enumerate(played[:-1]):
        try:
            board.play(player, pt)
            board.play(other(player), replies[i])
        except IllegalMove as exc:
            raise HTTPException(400, f"手顺非法：{exc}")

    last = played[-1]
    try:
        board.play(player, last)
    except IllegalMove as exc:
        raise HTTPException(400, str(exc))

    # 题型判据要从**初始局面**装配：life_oracle 靠 local_group 认「被判定的是哪块棋」，
    # 拿当前局面去算会把玩家刚落在范围内的那颗子也算进去，与出题口径不一致。
    oracle = None
    if rec.builtin:
        oracle = make_oracle(row_spec(rec), problem_board(rec.setup, rec.size))

    correct_line = _match(lines, "correct", played)
    wrong_line = None if correct_line else _match(lines, "wrong", played)

    reply: Optional[tuple[int, int]] = None
    refutation: list[tuple[int, int]] = []
    pv: list[list[int]] = []

    if correct_line is not None and len(played) < len(_player_moves(correct_line)):
        # 多手题：这一手对了，但还需要继续应对
        status = STATUS_CONTINUE
        comment = "对，继续应对。"
        reply = tuple(int(v) for v in correct_line["moves"][2 * len(played) - 1])
        try:
            board.play(other(player), reply)
        except IllegalMove as exc:
            raise HTTPException(400, f"题目变化线非法：{exc}")
    elif correct_line is not None:
        status = STATUS_SOLVED
        comment = correct_line.get("comment", "正解。")
        pv = [[int(m[0]), int(m[1])] for m in (correct_line.get("pv") or [])]
    elif wrong_line is not None:
        status = STATUS_FAILED
        comment = wrong_line.get("comment", "这一手不成立。")
        refutation = _norm(wrong_line["moves"][1:])
    elif rec.builtin or bool(area):
        # 变化线之外的落子（例如脱先到眼位外）：用搜索给出真实结论，
        # 而不是含糊地判错——内置题都是封闭局部；导入题（L18）只要在导入时
        # 推出了局部搜索范围（sgfimport._derive_space），同样可用搜索兜底，
        # 结论是「外接矩形局部的死活判定」，可信度比内置题低一档，文案里注明。
        import_ = not rec.builtin
        refutation = find_refutation(board, victim, goal, player, area,
                                     depth=DEPTH, oracle=oracle) or []
        # 搜索把「找劫材」抽象掉了，所以涉劫的应对可能含劫禁着；
        # 前端会直接拿这个列表画幽灵子，必须是真能摆出来的前缀
        refutation = replayable_prefix(board, other(player), refutation)
        verdict_now, _ = ld_search(board, victim, other(player), area,
                                   depth=DEPTH, oracle=oracle)
        tag = "（导入题局部搜索结果）" if import_ else ""
        if goal_achieved(verdict_now, goal):
            status = STATUS_SOLVED
            comment = f"{to_gtp(last, size)} 也能达成目标（与正解等价）。{tag}".strip()
        else:
            status = STATUS_FAILED
            comment = (f"{to_gtp(last, size)} 不成立："
                       f"{verdict_line(rec.kind, verdict_now, victim)}。{tag}").strip()
    else:
        status = STATUS_FAILED
        comment = "这一手不在题目的正解变化里。"

    # 把对方的最佳应对摆出来（教学用：让玩家看到自己为什么错）
    if refutation:
        color = other(player)
        for pt in refutation:
            if pt == (-1, -1):
                # 脱先哨兵（L3）：不落子，但著法权照常翻转
                color = other(color)
                continue
            try:
                board.play(color, pt)
            except IllegalMove:
                break
            color = other(color)

    verdict = ""
    if (rec.builtin or bool(area)) and status != STATUS_CONTINUE:
        if (status == STATUS_SOLVED and correct_line.get("verdict")
                and len(_player_moves(correct_line)) == 1):
            # 答对时**不必重算**：出题时 derive_problem 已经对同一个局面搜过一次，
            # 结论就存在变化线里。实测这次重搜要 ~100ms，而整个请求的框架 + 数据库
            # 开销只 ~13ms——也就是说重搜占了响应时间的九成（实测 108ms → 7ms）。
            #
            # 两个前提缺一不可，否则就是给学员看错的结论：
            #   ① 只限「走完正解线」且该线**只有一手**：那时 board 恰好等于
            #     setup + 玩家这一手，与出题时搜索的局面完全相同。当前全库 174 条
            #     正解线都是单手，但这是题库的**当前属性而不是代码保证**，所以显式判一下：
            #     将来真出现多手线（board 里还含着对手应手）就自动退回现场搜索；
            #   ② 答错的路不复用：那里 board 摆上了 refutation，局面已经不同。
            verdict = correct_line["verdict"]
        else:
            # 口径与出题一致：玩家下完这一手后轮到对方，此时的结论就是本题结果
            verdict, _ = ld_search(board, victim, other(player), area,
                                   depth=DEPTH, oracle=oracle)

    # 记录练习进度：一次判定 = 一次做题（多手题只在终局那步计数）
    prog = _progress(db, user, rec.id)
    if status != STATUS_CONTINUE:
        prog.attempts += 1
        if status == STATUS_SOLVED and not prog.solved:
            prog.solved = True
    db.commit()

    return {
        "status": status,
        "comment": comment,
        "board": [row[:] for row in board.grid],
        "verdict": verdict,
        "verdictText": verdict_line(rec.kind, verdict, victim) if verdict else "",
        "goalAchieved": goal_achieved(verdict, goal) if verdict else None,
        "moveText": to_gtp(last, size),
        "attempts": prog.attempts,
        "solved": prog.solved,
        "reply": [reply[0], reply[1]] if reply else None,
        "refutation": [[p[0], p[1]] for p in refutation],
        "pv": pv,
    }


@router.get("/{problem_id}/solution")
def solution(problem_id: str, user: User = Depends(get_current_user),
             db: Session = Depends(get_db)):
    """看答案：返回全部变化线，并记一次「看过答案」（不算解出）。"""
    rec = db.get(TsumegoProblem, problem_id)
    if rec is None or not rec.enabled:
        raise HTTPException(404, "题目不存在")
    prog = _progress(db, user, rec.id)
    prog.attempts += 1
    prog.seen_answer = True
    db.commit()
    size = rec.size
    lines = []
    for line in (rec.lines or []):
        lines.append({
            "result": line.get("result"),
            "moves": [[int(m[0]), int(m[1])] for m in line.get("moves", [])],
            "movesText": " ".join("脱先" if m[0] == -1 else to_gtp((int(m[0]), int(m[1])), size)
                                  for m in line.get("moves", [])),
            "pv": [[int(m[0]), int(m[1])] for m in line.get("pv", [])],
            "comment": line.get("comment", ""),
        })
    return {
        "id": rec.id,
        "note": rec.note,
        "hint": rec.hint,
        "source": rec.source,
        "kind": rec.kind or "life",
        "kindText": KIND_TEXT.get(rec.kind or "life", rec.kind or "life"),
        "goalText": goal_text(rec.goal),
        "difficulty": rec.difficulty,
        "tier": rec.tier or "入门",
        "victimText": color_name(rec.victim),
        "lines": lines,
        "attempts": prog.attempts,
        "solved": prog.solved,
    }


__all__ = ["router", "STATUS_CONTINUE", "STATUS_FAILED", "STATUS_SOLVED"]
