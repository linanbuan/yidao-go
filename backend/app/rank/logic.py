"""升降级状态机：胜场累计 → 晋升战（连胜 + 吻合度）→ 升段；连败可降级（默认关闭）。

所有变更都会写入 RankEvent 流水，前端可展示"成长时间线"。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..models import RankEvent, User
from .defs import (DEFAULT_RANK_ID, MAX_RANK_ID, MIN_RANK_ID, EngineProfile,
                   get_engine_profile, get_rank)


def promotion_profile(rank_id: int) -> EngineProfile:
    """晋升战对手 = **目标段位**的常规画像，而不是本级的满配 AI。

    旧实现取「本级 visits×2 + 三个削弱旋钮全部归零」，后果是难度断崖：
    18级 平时每手亏 ~4.3 目（新手能赢），晋升战却是零噪声零容差的满血 KataGo
    （每手亏 ~0 目，超人类），还要连胜 2 局 —— 玩家永远升不上去。

    「打赢你想升到的那一档」既公平又是真正的能力证明：目标档本身就比当前档
    强，而难度来自「连胜 2 局」的一致性要求，不是来自一个超人对手。
    搜索量小幅上调 + 开 ponder，让晋升战比平时「认真」一点，但不改变棋力档位。
    九段已是顶格，目标就是它自己。
    """
    p = get_engine_profile(min(MAX_RANK_ID, rank_id + 1))
    return replace(p, max_visits=min(3200, int(p.max_visits * 1.5)), ponder=True)


@dataclass
class RankProgress:
    rank_id: int
    rank_name: str
    short: str
    ai_name: str
    ai_title: str
    elo: int
    rank_wins: int
    wins_required: int
    rank_losses: int
    win_streak: int
    losing_streak: int
    in_promotion: bool
    promotion_wins: int
    promotion_required: int
    max_avg_loss_points: Optional[float]
    promo_accuracy: Optional[float]
    total_games: int
    total_wins: int
    accuracy_avg: float
    is_max_rank: bool
    hint: str

    def to_dict(self) -> dict:
        """输出 camelCase（与其余 API / 前端一致）。"""
        return {
            "rankId": self.rank_id,
            "rankName": self.rank_name,
            "short": self.short,
            "aiName": self.ai_name,
            "aiTitle": self.ai_title,
            "elo": self.elo,
            "rankWins": self.rank_wins,
            "winsRequired": self.wins_required,
            "rankLosses": self.rank_losses,
            "winStreak": self.win_streak,
            "losingStreak": self.losing_streak,
            "inPromotion": self.in_promotion,
            "promotionWins": self.promotion_wins,
            "promotionRequired": self.promotion_required,
            # 键名带单位语义：这些值都是**目/手**（平均每手损失目数），不是百分比（§3.16）
            "maxAvgLossPoints": self.max_avg_loss_points,
            "promoAvgLossPoints": self.promo_accuracy,
            "totalGames": self.total_games,
            "totalWins": self.total_wins,
            "avgLossPoints": self.accuracy_avg,
            "isMaxRank": self.is_max_rank,
            "hint": self.hint,
        }


def progress_of(user: User) -> RankProgress:
    r = get_rank(user.rank_id)
    promo_acc = (user.promo_accuracy_sum / user.promo_accuracy_n) if user.promo_accuracy_n else None
    if r.rank_id >= MAX_RANK_ID:
        hint = "已达九段（最高段位），继续对局保持手感"
    elif user.in_promotion:
        need = max(0, r.promo_streak - user.promotion_wins)
        hint = f"晋升战进行中：再连胜 {need} 场即可升为{get_rank(r.rank_id + 1).name}"
        if r.max_avg_loss_points is not None:
            hint += f"（吻合度要求：平均每手损失 ≤ {r.max_avg_loss_points} 目）"
    else:
        need = max(0, r.wins_required - user.rank_wins)
        hint = f"再赢 {need} 场即可进入晋升战" if need else "下一胜即进入晋升战"
    return RankProgress(
        rank_id=r.rank_id, rank_name=r.name, short=r.short, ai_name=r.ai_name,
        ai_title=r.ai_title, elo=r.elo, rank_wins=user.rank_wins,
        wins_required=r.wins_required, rank_losses=user.rank_losses,
        win_streak=user.win_streak, losing_streak=user.losing_streak,
        in_promotion=user.in_promotion, promotion_wins=user.promotion_wins,
        promotion_required=r.promo_streak, max_avg_loss_points=r.max_avg_loss_points,
        promo_accuracy=round(promo_acc, 2) if promo_acc is not None else None,
        total_games=user.total_games, total_wins=user.total_wins,
        accuracy_avg=round(user.accuracy_avg, 2), is_max_rank=r.rank_id >= MAX_RANK_ID,
        hint=hint,
    )


def _add_event(db: Session, user: User, kind: str, from_rank: int, to_rank: int,
               game_id: str, detail: str) -> RankEvent:
    ev = RankEvent(user_id=user.id, kind=kind, from_rank=from_rank, to_rank=to_rank,
                   game_id=game_id, detail=detail)
    db.add(ev)
    return ev


def on_game_finished(db: Session, user: User, *, won: bool, game_id: str = "",
                     accuracy: Optional[float] = None,
                     is_promotion_game: bool = False) -> dict:
    """对局结束后更新等级状态。返回 {events, promoted, demoted, progress}。"""
    rank = get_rank(user.rank_id)
    events: list[RankEvent] = []
    promoted = demoted = False
    promo_failed = False

    user.total_games += 1
    if won:
        user.total_wins += 1
    # 吻合度滚动平均（每手平均损失目数）
    if accuracy is not None:
        n = max(1, user.total_games)
        user.accuracy_avg = round(
            (user.accuracy_avg * (n - 1) + accuracy) / n, 3)

    demotion_on = user.demotion_enabled or settings.demotion_enabled

    if won:
        user.losing_streak = 0
        user.win_streak += 1
        user.rank_wins += 1

        # 吻合度校验（高段晋升战要求）
        acc_ok = True
        if is_promotion_game or user.in_promotion:
            if accuracy is not None and rank.max_avg_loss_points is not None:
                user.promo_accuracy_sum += accuracy
                user.promo_accuracy_n += 1
                avg = user.promo_accuracy_sum / user.promo_accuracy_n
                if avg > rank.max_avg_loss_points:
                    acc_ok = False

        if user.in_promotion:
            if not acc_ok:
                # 吻合度不达标：晋升战失败，退回累计胜场阶段
                promo_failed = True
                user.in_promotion = False
                user.promotion_wins = 0
                user.promo_accuracy_sum = 0.0
                user.promo_accuracy_n = 0
                user.rank_wins = max(0, rank.wins_required - 1)
                avg = accuracy if accuracy is not None else 0.0
                events.append(_add_event(
                    db, user, "promo_fail", rank.rank_id, rank.rank_id, game_id,
                    f"晋升战吻合度未达标（平均每手损失 {avg:.2f} 目 > {rank.max_avg_loss_points} 目）"))
            else:
                user.promotion_wins += 1
                if user.promotion_wins >= rank.promo_streak and rank.rank_id < MAX_RANK_ID:
                    new_rank = rank.rank_id + 1
                    events.append(_add_event(
                        db, user, "promote", rank.rank_id, new_rank, game_id,
                        f"晋升战 {user.promotion_wins} 连胜，升为{get_rank(new_rank).name}"))
                    user.rank_id = new_rank
                    user.best_rank_id = max(user.best_rank_id, new_rank)
                    user.rank_wins = 0
                    user.rank_losses = 0
                    user.promotion_wins = 0
                    user.in_promotion = False
                    user.promo_accuracy_sum = 0.0
                    user.promo_accuracy_n = 0
                    user.win_streak = 0
                    promoted = True
                elif rank.rank_id >= MAX_RANK_ID:
                    user.promotion_wins = rank.promo_streak   # 封顶
        else:
            if user.rank_wins >= rank.wins_required and rank.rank_id < MAX_RANK_ID:
                user.in_promotion = True
                user.promotion_wins = 0
                user.promo_accuracy_sum = 0.0
                user.promo_accuracy_n = 0
                events.append(_add_event(
                    db, user, "promo_start", rank.rank_id, rank.rank_id, game_id,
                    f"累计 {user.rank_wins} 胜，进入晋升战（需 {rank.promo_streak} 连胜）"))
    else:
        user.win_streak = 0
        user.losing_streak += 1
        user.rank_losses += 1
        if user.in_promotion:
            promo_failed = True
            user.in_promotion = False
            user.promotion_wins = 0
            user.promo_accuracy_sum = 0.0
            user.promo_accuracy_n = 0
            user.rank_wins = max(0, rank.wins_required - 1)
            events.append(_add_event(
                db, user, "promo_fail", rank.rank_id, rank.rank_id, game_id,
                "晋升战中断（连败），需再赢 1 场重新进入"))
        elif demotion_on and user.losing_streak >= settings.demotion_losing_streak \
                and user.rank_id > MIN_RANK_ID:
            new_rank = user.rank_id - 1
            events.append(_add_event(
                db, user, "demote", rank.rank_id, new_rank, game_id,
                f"连败 {user.losing_streak} 场，降为{get_rank(new_rank).name}"))
            user.rank_id = new_rank
            user.rank_wins = 0
            user.rank_losses = 0
            user.losing_streak = 0
            demoted = True

    db.flush()
    return {
        "promoted": promoted,
        "demoted": demoted,
        "promoFailed": promo_failed,
        "events": [{"kind": e.kind, "fromRank": e.from_rank, "toRank": e.to_rank,
                    "detail": e.detail} for e in events],
        "progress": progress_of(user).to_dict(),
    }


def init_user_rank(user: User) -> None:
    """新用户初始化到 18级。"""
    user.rank_id = DEFAULT_RANK_ID
    user.best_rank_id = DEFAULT_RANK_ID
