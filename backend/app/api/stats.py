"""统计 API：日历形式的历史对局数量与每日胜率。"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from ..database import get_db
from ..models import GameRecord, User
from .deps import get_current_user

router = APIRouter(prefix="/api/stats", tags=["stats"])


def _local_tz():
    """服务器本地时区。日历是按「人过的那一天」分的，不是按 UTC 分的。"""
    return datetime.now().astimezone().tzinfo


def _to_local(dt: datetime):
    """DB 里的 created_at 是 naive UTC（SQLite 的 DateTime 不保存 tzinfo）。

    必须显式补上 UTC 再转本地：直接 astimezone() 会把 naive 值当成本地时间，
    于是东八区凌晨 0~8 点的对局全部被记到前一天 —— 日历看着少一天，很难查。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_local_tz())


def _month_window_utc(year: int, month: int) -> tuple[datetime, datetime]:
    """本地月份 [起, 止) 对应的 naive UTC 区间，用来把扫描范围压到一个月内。"""
    tz = _local_tz()
    start = datetime(year, month, 1, tzinfo=tz)
    end = (datetime(year + 1, 1, 1, tzinfo=tz) if month == 12
           else datetime(year, month + 1, 1, tzinfo=tz))
    return (start.astimezone(timezone.utc).replace(tzinfo=None),
            end.astimezone(timezone.utc).replace(tzinfo=None))


def _classify(rec: GameRecord) -> str:
    """把一条记录归到 win / loss / noResult / ongoing。

    winner == 0 就是「没有胜方」：强制结束的作废局、导入的棋谱都属于这类。
    这类局计入「下过几盘」，但**不进胜率的分母** —— 否则强制结束就成了
    洗掉败场、抬高胜率的按钮。
    """
    if not rec.finished:
        return "ongoing"
    if rec.winner:
        return "win" if rec.player_won else "loss"
    return "noResult"


@router.get("/calendar")
def calendar(year: Optional[int] = Query(default=None, ge=2000, le=2100),
             month: Optional[int] = Query(default=None, ge=1, le=12),
             user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """某月每天下了几盘、胜率多少。不传 year/month 则取本地当前月。

    统计口径基于**对局记录**，所以删掉的记录会从日历上消失；但 lifetime 那一段
    取自 User 上的累计字段，删除记录不会动它 —— 这正是「清空历史、只保留胜负
    场数与总胜率」的预期效果。
    """
    today = datetime.now(_local_tz())
    year = year or today.year
    month = month or today.month
    start_utc, end_utc = _month_window_utc(year, month)
    rows = db.scalars(
        select(GameRecord)
        .options(load_only(GameRecord.created_at, GameRecord.finished,
                           GameRecord.winner, GameRecord.player_won))
        .where(GameRecord.user_id == user.id,
               GameRecord.created_at >= start_utc,
               GameRecord.created_at < end_utc)
    ).all()

    buckets: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    totals: dict[str, int] = defaultdict(int)
    for rec in rows:
        created = rec.created_at or datetime.now(timezone.utc).replace(tzinfo=None)
        day = _to_local(created).date()
        kind = _classify(rec)
        buckets[day][kind] += 1
        buckets[day]["games"] += 1
        totals[kind] += 1
        totals["games"] += 1

    days = {}
    for day, b in sorted(buckets.items()):
        decided = b["win"] + b["loss"]
        days[day.isoformat()] = {
            "games": b["games"], "wins": b["win"], "losses": b["loss"],
            "noResult": b["noResult"], "ongoing": b["ongoing"],
            "winrate": round(b["win"] / decided, 4) if decided else None,
        }

    decided_total = totals["win"] + totals["loss"]
    tz = _local_tz()
    now_local = datetime.now(tz)
    offset = now_local.utcoffset() or timedelta(0)
    sign = "+" if offset >= timedelta(0) else "-"
    hours, minutes = divmod(abs(int(offset.total_seconds())) // 60, 60)
    return {
        "year": year, "month": month,
        "timezone": f"{now_local.tzname() or 'local'} (UTC{sign}{hours:02d}:{minutes:02d})",
        "days": days,
        "totals": {
            "games": totals["games"], "wins": totals["win"], "losses": totals["loss"],
            "noResult": totals["noResult"], "ongoing": totals["ongoing"],
            "activeDays": len(days),
            "winrate": round(totals["win"] / decided_total, 4) if decided_total else None,
        },
        # 生涯累计取自 User，与记录条数无关（记录可以被删，战绩不会被删）
        "lifetime": {
            "totalGames": user.total_games,
            "totalWins": user.total_wins,
            "totalLosses": max(0, user.total_games - user.total_wins),
            "winrate": round(user.total_wins / user.total_games, 4) if user.total_games else 0.0,
            "rankWins": user.rank_wins,
            "rankLosses": user.rank_losses,
            # 是「当前」连胜/连败，不是历史最高 —— User 上没有存过最高连胜
            "winStreak": user.win_streak,
            "losingStreak": user.losing_streak,
        },
    }
