"""对局 REST API（WebSocket 之外的操作入口，便于前端降级与测试）。"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..game.manager import get_hub, record_brief
from ..game.rules import BLACK, IllegalMove, WHITE
from ..game.sgf import export_sgf, parse_sgf, sgf_to_game
from ..models import GameRecord, RankEvent, User
from ..rank.defs import get_rank
from .deps import get_current_user

router = APIRouter(prefix="/api/games", tags=["games"])
logger = logging.getLogger("go.api.games")


class CreateGameIn(BaseModel):
    size: int = Field(default=19, ge=9, le=19)
    komi: float = 7.5
    handicap: int = Field(default=0, ge=0, le=9)
    # 执子：1=黑、2=白、**0=抽取（猜先，由服务端随机决定）**。
    # 抽取必须放在服务端：放前端的话用户可以反复抽到满意为止，那就不是猜先了。
    # 让子棋（handicap>=2）不论传什么都固定玩家执黑，见 manager.GameHub.create。
    # 导入棋谱的 ImportIn.playerColor 不接受 0：那是已下完的局，执什么是事实而不是待抽。
    playerColor: int = Field(default=BLACK, ge=0, le=2)
    scoreMethod: str = "area"          # area=数子 territory=数目
    hintMode: Optional[bool] = None
    # 每手限时秒数，0=不限时；不传则用 settings.move_seconds_default
    moveSeconds: Optional[int] = Field(default=None, ge=0, le=3600)


class ScoreIn(BaseModel):
    dead: Optional[list[list[int]]] = None


class ImportIn(BaseModel):
    sgf: str
    playerColor: int = Field(default=BLACK, ge=1, le=2)
    aiName: str = "导入棋谱"


def _next_color(rec: GameRecord, live) -> int:
    """当前该谁落子（活对局直接取，已结束/未加载则按手顺推算）。"""
    if live is not None:
        return live.game.next_color
    first = WHITE if rec.handicap >= 2 else BLACK
    n = len(rec.moves or [])
    return first if n % 2 == 0 else (WHITE if first == BLACK else BLACK)


def _owned(rec: Optional[GameRecord], user: User) -> GameRecord:
    if rec is None:
        raise HTTPException(404, "对局不存在")
    if rec.user_id != user.id:
        raise HTTPException(403, "无权访问该对局")
    return rec


def _detail_payload(rec: GameRecord, live=None) -> dict:
    rank = get_rank(rec.rank_id)
    payload = {
        "id": rec.id,
        "createdAt": rec.created_at.isoformat() if rec.created_at else "",
        "size": rec.size, "komi": rec.komi, "handicap": rec.handicap,
        "playerColor": rec.player_color, "colorSource": rec.color_source,
        "scoreMethod": rec.score_method, "koRule": rec.ko_rule,
        "allowTakeback": rec.allow_takeback,
        "moveSeconds": rec.move_seconds or 0,
        "rankId": rec.rank_id, "rankName": rank.name,
        "aiName": rec.ai_name, "aiTitle": rank.ai_title,
        "isPromotion": rec.is_promotion,
        "status": rec.status, "finished": rec.finished,
        "finishReason": rec.finish_reason,
        "winner": rec.winner, "resultText": rec.result_text,
        "playerWon": rec.player_won, "result": rec.result_json,
        "moves": rec.moves or [], "analyses": rec.analyses or [],
        "moveCount": len(rec.moves or []),
        "nextColor": _next_color(rec, live),
        "takebackCount": rec.takeback_count,
        "engine": rec.engine_name,
        "reviewStatus": rec.review_status,
        "reviewError": rec.review_error,
        # 单位是**目/手**（平均每手损失目数），不是百分比 —— 键名不用 accuracy，
        # 免得前端按吻合度百分比渲染（审计 §3.16）。
        "avgLossPoints": rec.accuracy,
        "llmUsed": rec.llm_used,
        "sgf": rec.sgf,
    }
    if live is not None:
        payload["phase"] = live.phase
        payload["scoringDead"] = [[p[0], p[1]] for p in live.scoring_dead]
        payload["despairPlies"] = live.despair_plies
        payload["moveSecondsLeft"] = live.seconds_left()
        payload["curve"] = live.curve_payload()
        payload["hintMode"] = live.hint_mode
    else:
        payload["phase"] = "finished" if rec.finished else rec.status
        payload["scoringDead"] = []
        payload["curve"] = [{
            "ply": i, "winrateBlack": a.get("winrateBlack"),
            "winrateWhite": a.get("winrateWhite"), "scoreLead": a.get("scoreLead"),
            "visits": a.get("visits"),
            "color": (rec.moves[i - 1].get("color") if 0 < i <= len(rec.moves or []) else None),
        } for i, a in enumerate(rec.analyses or []) if not a.get("missing")]
    return payload


@router.post("")
async def create_game(body: CreateGameIn, user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    if body.size not in (9, 13, 19):
        raise HTTPException(400, "棋盘仅支持 9/13/19 路")
    if not (0.0 <= body.komi <= 30.0):
        raise HTTPException(400, "贴目需在 0~30 之间")
    if body.scoreMethod not in ("area", "territory"):
        raise HTTPException(400, "scoreMethod 仅支持 area / territory")
    hub = get_hub()
    active = hub.get_active_for_user(user.id)
    if active is None:
        # 服务重启后 hub 是空的，而 DB 里还挂着 status=playing/scoring 的旧局。
        # 只查内存就会放行新对局 → 同一用户两盘「进行中」各自飞行。
        # 这里把旧局 restore 回内存（而不是直接 409 死锁）：用户能继续或删掉它，
        # 同时 409 照常挡住「同时开两盘」。
        stale = db.scalars(
            select(GameRecord)
            .where(GameRecord.user_id == user.id,
                   GameRecord.status.in_(("playing", "scoring")))
            .order_by(GameRecord.created_at.desc())
        ).first()
        if stale is not None:
            active = await hub.restore(stale.id, user_hint_mode=user.hint_mode)
    if active is not None:
        raise HTTPException(409, f"已有进行中的对局（{active.id}），请先结束或继续")
    live = hub.create(user, size=body.size, komi=body.komi, handicap=body.handicap,
                      player_color=body.playerColor, score_method=body.scoreMethod,
                      hint_mode=body.hintMode, move_seconds=body.moveSeconds)
    db.refresh(user)
    rec = db.get(GameRecord, live.id)
    payload = _detail_payload(rec, live)

    # 让子棋或玩家执白时，AI 先走
    first_events = []
    if live.game.next_color != live.player_color:
        from ..engine.pool import get_pool
        query = live.build_query()
        result = await get_pool().analyze(query, side_to_move=live.ai_color, turn=0,
                                          profile=live.profile)
        if live.abandoned:
            # await 引擎的几秒里对局被强制结束：别再往已作废的局面落子
            payload = _detail_payload(db.get(GameRecord, live.id), live)
            return {"game": payload, "events": [], "ws": f"/ws/game/{live.id}"}
        live.record_analysis(result)
        point = get_pool().choose_move(live.game.board, live.ai_color, result, live.profile)
        move = live.game.play(live.ai_color, point)
        # AI 先手落完后开始玩家第一手计时（创建时 next_color 还是 AI，arm 被短路了）
        hub.arm_player_clock(live)
        first_events.append({"type": "analysis", "index": 0,
                             "analysis": live.analyses[-1] if live.analyses else None})
        first_events.append({"type": "aiMove", "move": move.to_dict(live.game.size),
                             "moveCount": len(live.game.moves),
                             "moveSecondsLeft": live.seconds_left()})
        hub.save(live)
        # hub.save 用的是它自己的 session，本函数这个 session 的 identity map 里还是
        # 创建时那份 rec（moves 为空），db.get 不会重查库。不 refresh 的话返回体
        # 自相矛盾：nextColor 说轮到玩家、events 里带着 aiMove，moveCount 却是 0。
        # （实测：玩家执白或让子棋时 AI 先落一子，GET 详情是 1 手、创建返回体是 0 手。）
        rec = db.get(GameRecord, live.id)
        db.refresh(rec)
        payload = _detail_payload(rec, live)
    return {"game": payload, "events": first_events,
            "ws": f"/ws/game/{live.id}"}


@router.get("")
def list_games(limit: int = Query(default=30, ge=1, le=100),
               offset: int = Query(default=0, ge=0),
               user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    items, total = get_hub().list_for_user(user.id, limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/active")
def active_game(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    hub = get_hub()
    live = hub.get_active_for_user(user.id)
    if live is None:
        return {"game": None}
    rec = _owned(db.get(GameRecord, live.id), user)
    return {"game": _detail_payload(rec, live)}


@router.get("/{game_id}")
async def get_game(game_id: str, user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    rec = _owned(db.get(GameRecord, game_id), user)
    live = get_hub().get(game_id)
    if live is None and rec.status in ("playing", "scoring"):
        live = await get_hub().restore(game_id, user_hint_mode=user.hint_mode)
    return {"game": _detail_payload(rec, live)}


@router.post("/{game_id}/takeback")
async def takeback(game_id: str, plies: int = Query(default=2, ge=1, le=100),
                   user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    rec = _owned(db.get(GameRecord, game_id), user)
    live = get_hub().get(game_id) or await get_hub().restore(game_id, user.hint_mode)
    if live is None:
        raise HTTPException(404, "对局未在内存中，无法悔棋")
    try:
        return {"event": await get_hub().takeback(live, plies)}
    except IllegalMove as exc:
        raise HTTPException(400, str(exc))


@router.post("/{game_id}/resign")
async def resign(game_id: str, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    rec = _owned(db.get(GameRecord, game_id), user)
    if rec.finished:
        raise HTTPException(400, "对局已结束")
    live = get_hub().get(game_id) or await get_hub().restore(game_id, user.hint_mode)
    if live is None:
        raise HTTPException(404, "对局未在内存中")
    event = get_hub().player_resign(live)
    live.emit(event)
    return {"event": event}


@router.post("/{game_id}/score")
async def confirm_score(game_id: str, body: ScoreIn, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    rec = _owned(db.get(GameRecord, game_id), user)
    live = get_hub().get(game_id) or await get_hub().restore(game_id, user.hint_mode)
    if live is None:
        raise HTTPException(404, "对局未在内存中")
    if live.phase != "scoring":
        raise HTTPException(400, "当前不在终局结算阶段")
    dead = [tuple(p) for p in (body.dead or [])] if body.dead is not None else None
    try:
        event = get_hub().confirm_score(live, dead)
    except IllegalMove as exc:
        # 死子坐标非法：400 而不是 500，也绝不带着坏坐标去判定胜负
        raise HTTPException(400, str(exc))
    live.emit(event)
    return {"event": event}


@router.post("/{game_id}/resume")
async def resume(game_id: str, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    rec = _owned(db.get(GameRecord, game_id), user)
    live = get_hub().get(game_id) or await get_hub().restore(game_id, user.hint_mode)
    if live is None:
        raise HTTPException(404, "对局未在内存中")
    try:
        event = get_hub().resume_from_scoring(live)
    except IllegalMove as exc:
        raise HTTPException(400, str(exc))
    live.emit(event)
    return {"event": event}


@router.post("/{game_id}/force-end")
async def force_end(game_id: str, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    """强制结束对局（不计入胜负）。

    只允许对**尚未结束**的对局用。已经正常终局的棋局再强制结束，会把一条真实
    败局改写成「无胜负」，而败局早已计入 User.total_wins/total_losses —— 结果是
    总战绩说输过、日历胜率却把它从分母里抹掉，两处数字对不上。想要作废一盘
    已经下完的棋，用删除记录（它同样保留胜负场数）。
    """
    rec = _owned(db.get(GameRecord, game_id), user)
    if rec.finished:
        raise HTTPException(400, "对局已经结束，不能再强制结束（已计入的胜负不会被抹除）；"
                                 "如需作废这一盘请直接删除记录")
    hub = get_hub()
    live = hub.get(game_id) or await hub.restore(game_id, user_hint_mode=user.hint_mode)
    event = hub.force_end(rec, live)
    db.commit()
    db.refresh(rec)
    return {"event": event, "game": record_brief(rec)}


@router.delete("/{game_id}")
def delete_game(game_id: str, user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    """删除单条对战记录。

    **只删明细，不动战绩**：User 上的 total_games / total_wins / rank_wins /
    rank_losses / win_streak / accuracy_avg 全部保留。这是故意的：本级胜场驱动着
    晋升进度，删一盘棋就把晋升进度回退，对学员是灾难性的体验。
    代价是「总战绩 50 胜」与「列表里只剩 10 条」会共存，属于预期行为；
    日历统计按记录算，所以被删掉的那些天会从日历上消失。
    """
    rec = _owned(db.get(GameRecord, game_id), user)
    hub = get_hub()
    live = hub.get(game_id)
    if live is not None:
        # 还开着页面的客户端得知道对局没了，否则会接着往一个已删除的对局上落子
        live.abandoned = True       # 同时拦住在飞的 AI 回合，别给已删的对局记胜负
        live.emit({"type": "error", "message": "该对局记录已被删除"})
        hub.remove(game_id)
    _unlink_timeline(db, [game_id])
    db.delete(rec)
    db.commit()
    return {"deleted": 1, **_stats_payload(db, user)}


@router.post("/clear-history")
def clear_history(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """清空历史对战记录，只保留胜负场数与总胜率。

    跳过未结束的对局（批量删掉一盘正在下的棋太意外）；要清掉它先用强制结束。
    """
    hub = get_hub()
    rows = db.scalars(select(GameRecord).where(GameRecord.user_id == user.id)).all()
    deleted, skipped, gone = 0, 0, []
    for rec in rows:
        if rec.status in ("playing", "scoring"):
            skipped += 1
            continue
        live = hub.get(rec.id)
        if live is not None:
            live.abandoned = True
            hub.remove(rec.id)
        gone.append(rec.id)
        db.delete(rec)
        deleted += 1
    _unlink_timeline(db, gone)
    db.commit()
    return {"deleted": deleted, "skipped": skipped, **_stats_payload(db, user)}


def _unlink_timeline(db: Session, game_ids: list[str]) -> None:
    """断开成长时间线对已删对局的引用。

    RankEvent.game_id 没有外键约束，删了对局它也不会跟着清，于是大厅时间线上的
    「复盘」按钮会变成 404。置空后前端就不渲染那个按钮（它本来就按 gameId 判断）。
    """
    if not game_ids:
        return
    for ev in db.scalars(select(RankEvent).where(RankEvent.game_id.in_(game_ids))).all():
        ev.game_id = ""


def _stats_payload(db: Session, user: User) -> dict:
    """删完之后把「战绩还在」一并返回，前端可以直接拿这句话告知用户。"""
    db.refresh(user)
    total = user.total_games
    return {
        "remaining": int(db.scalar(select(func.count(GameRecord.id))
                                   .where(GameRecord.user_id == user.id)) or 0),
        "stats": {
            "totalGames": total,
            "totalWins": user.total_wins,
            "totalLosses": max(0, total - user.total_wins),
            "winrate": round(user.total_wins / total, 4) if total else 0.0,
        },
    }


@router.get("/{game_id}/sgf", response_class=PlainTextResponse)
def get_sgf(game_id: str, user: User = Depends(get_current_user),
            db: Session = Depends(get_db)):
    rec = _owned(db.get(GameRecord, game_id), user)
    rank = get_rank(rec.rank_id)
    if rec.sgf:
        return rec.sgf
    game = sgf_from_record(rec)
    return export_sgf(game,
                      black_name="玩家" if rec.player_color == BLACK else rank.ai_name,
                      white_name=rank.ai_name if rec.player_color == BLACK else "玩家")


def sgf_from_record(rec: GameRecord):
    """用对局记录重建 Game 对象（导出 SGF 用）。"""
    from ..game.rules import Game
    g = Game(size=rec.size, komi=rec.komi, handicap=rec.handicap,
             score_method=rec.score_method, ko_rule=rec.ko_rule,
             player_color=rec.player_color)
    if g.handicap_stones:
        g.next_color = WHITE
    for m in rec.moves or []:
        point = None if m.get("x") is None else (int(m["x"]), int(m["y"]))
        try:
            g.play(int(m["color"]), point)
        except IllegalMove as exc:
            logger.warning("重建对局 %s 失败：第 %d 手非法（%s），棋谱被截断",
                           rec.id, len(g.moves) + 1, exc)
            break
    g.finished = bool(rec.finished)
    g.finish_reason = rec.finish_reason or None
    g.result = rec.result_json or None
    return g


@router.post("/import")
def import_sgf(body: ImportIn, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    """导入 SGF 棋谱：生成一条已结束的对局记录并自动进入复盘队列。"""
    try:
        data = parse_sgf(body.sgf)
        game = sgf_to_game(body.sgf)
    except Exception as exc:   # noqa: BLE001
        raise HTTPException(400, f"SGF 解析失败: {exc}")
    rec = GameRecord(
        user_id=user.id, size=game.size, komi=game.komi, handicap=game.handicap,
        player_color=body.playerColor, color_source="import", score_method=game.score_method,
        allow_takeback=False, rank_id=user.rank_id, ai_name=body.aiName,
        is_promotion=False, engine_name="imported", status="finished", finished=True,
        finish_reason="imported", winner=0, result_text=data.get("result") or "导入棋谱",
        moves=[m.to_dict(game.size) for m in game.moves],
        sgf=body.sgf, review_status="none",
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    from ..review.worker import enqueue_review
    enqueue_review(rec.id)
    return {"gameId": rec.id, "moveCount": len(game.moves), "size": game.size,
            "reviewStatus": rec.review_status}
