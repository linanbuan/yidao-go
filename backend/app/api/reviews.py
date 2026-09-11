"""复盘报告 API：查询、触发重算、导出 Markdown。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..game.rules import BLACK
from ..models import GameRecord, User
from ..rank.defs import get_rank
from ..review.exporter import review_to_markdown
from ..review.worker import enqueue_review, review_progress
from .deps import get_current_user

router = APIRouter(prefix="/api/reviews", tags=["review"])


def _owned(rec, user: User) -> GameRecord:
    if rec is None:
        raise HTTPException(404, "对局不存在")
    if rec.user_id != user.id:
        raise HTTPException(403, "无权访问该对局")
    return rec


#: 旧版报告里这两项叫 accuracy / aiAccuracy —— 名字容易被前端当成百分比，
#: 而它的单位其实是**目/手**（平均每手损失目数，0~19 量级）。§3.16 改名后，
#: 已落库的旧报告仍在库里用旧键，这里在**读出口**统一翻成新键，客户端不必兼容两套。
_LEGACY_REPORT_KEYS = {"accuracy": "avgLossPoints", "aiAccuracy": "aiAvgLossPoints"}


def _normalize_report(raw: Optional[dict]) -> Optional[dict]:
    if not raw:
        return None
    out = dict(raw)
    for old, new in _LEGACY_REPORT_KEYS.items():
        if old in out:
            out.setdefault(new, out.pop(old))
    return out


@router.get("/{game_id}/status")
async def status(game_id: str, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    rec = _owned(db.get(GameRecord, game_id), user)
    return {"gameId": game_id, **await review_progress(game_id)}


@router.get("/{game_id}")
def get_report(game_id: str, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    rec = _owned(db.get(GameRecord, game_id), user)
    rank = get_rank(rec.rank_id)
    meta = {
        "gameId": rec.id,
        "createdAt": rec.created_at.isoformat() if rec.created_at else "",
        "size": rec.size, "komi": rec.komi, "handicap": rec.handicap,
        "playerColor": rec.player_color, "colorSource": rec.color_source,
        "playerColorName": "黑" if rec.player_color == BLACK else "白",
        "rankId": rec.rank_id, "rankName": rank.name,
        "aiName": rec.ai_name, "aiTitle": rank.ai_title,
        "isPromotion": rec.is_promotion,
        "resultText": rec.result_text, "playerWon": rec.player_won,
        "finishReason": rec.finish_reason,
        "moveCount": len(rec.moves or []),
        "reviewStatus": rec.review_status,
        "reviewProgress": 1.0 if rec.review_status == "done" else (rec.review_progress or 0.0),
        "reviewStage": rec.review_stage or "",
        "reviewDetail": rec.review_detail or "",
        "reviewError": rec.review_error,
        # 目/手（平均每手损失目数），非百分比（§3.16）
        "avgLossPoints": rec.accuracy,
        "engine": rec.engine_name,
    }
    return {
        "meta": meta,
        "report": _normalize_report(rec.review_json),
        "moves": rec.moves or [],
        "analyses": rec.analyses or [],
        "sgf": rec.sgf,
    }


@router.post("/{game_id}")
def rerun(game_id: str, user: User = Depends(get_current_user),
          db: Session = Depends(get_db)):
    """手动触发/重跑复盘（例如刚配置好大模型 Key 后）。"""
    rec = _owned(db.get(GameRecord, game_id), user)
    if not rec.moves:
        raise HTTPException(400, "空对局无法复盘")
    if rec.review_status == "pending":
        # 已在队列里 / 正在跑：再排一次只会白烧 19~45 秒（连点 N 次 = N 遍重算）
        return {"gameId": game_id, "reviewStatus": "pending", "dedup": True}
    rec.review_status = "pending"
    # 进度归零：worker 也会重置，但那要等它从队列里取到这一局；
    # 不先清的话前端会看到「停在 100% 却在转圈」
    rec.review_progress = 0.0
    rec.review_stage = "queued"
    rec.review_detail = "排队中"
    rec.review_error = ""
    db.commit()
    enqueue_review(game_id)
    return {"gameId": game_id, "reviewStatus": "pending"}


@router.get("/{game_id}/export")
def export_report(game_id: str, user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    rec = _owned(db.get(GameRecord, game_id), user)
    if not rec.review_json:
        raise HTTPException(404, "复盘报告尚未生成")
    md = review_to_markdown(rec.review_json, sgf=rec.sgf or "")
    filename = f"review_{game_id[:8]}.md"
    return PlainTextResponse(
        md, media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
