"""SQLAlchemy 模型：用户、对局、等级流水。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base
from .rank.defs import DEFAULT_RANK_ID


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    display_name: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    # ---- 等级与晋升 ----
    rank_id: Mapped[int] = mapped_column(Integer, default=DEFAULT_RANK_ID, index=True)
    best_rank_id: Mapped[int] = mapped_column(Integer, default=DEFAULT_RANK_ID)
    rank_wins: Mapped[int] = mapped_column(Integer, default=0)      # 本级累计胜场
    rank_losses: Mapped[int] = mapped_column(Integer, default=0)    # 本级累计负场
    win_streak: Mapped[int] = mapped_column(Integer, default=0)
    losing_streak: Mapped[int] = mapped_column(Integer, default=0)
    in_promotion: Mapped[bool] = mapped_column(Boolean, default=False)   # 是否已进入晋升战
    promotion_wins: Mapped[int] = mapped_column(Integer, default=0)      # 晋升战连胜进度
    total_games: Mapped[int] = mapped_column(Integer, default=0)
    total_wins: Mapped[int] = mapped_column(Integer, default=0)
    accuracy_avg: Mapped[float] = mapped_column(Float, default=0.0)      # 平均每手损失目数
    promo_accuracy_sum: Mapped[float] = mapped_column(Float, default=0.0)  # 晋升战期间累计
    promo_accuracy_n: Mapped[int] = mapped_column(Integer, default=0)

    # ---- 偏好 ----
    hint_mode: Mapped[bool] = mapped_column(Boolean, default=True)       # 对局中提示
    demotion_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    llm_config: Mapped[dict] = mapped_column(JSON, default=dict)         # 用户级 LLM 覆盖配置

    games = relationship("GameRecord", back_populates="user", cascade="all, delete-orphan")

    @property
    def name(self) -> str:
        return self.display_name or self.username


class GameRecord(Base):
    __tablename__ = "games"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    size: Mapped[int] = mapped_column(Integer, default=19)
    komi: Mapped[float] = mapped_column(Float, default=7.5)
    handicap: Mapped[int] = mapped_column(Integer, default=0)
    player_color: Mapped[int] = mapped_column(Integer, default=1)
    # 执子来源（L15）：pick 自选 / guess 猜先 / rule 让子规则强制 / import 导入棋谱。
    # 旧行无从考证一律算自选（迁移时补默认值）。
    color_source: Mapped[str] = mapped_column(String(16), default="pick")
    score_method: Mapped[str] = mapped_column(String(16), default="area")
    ko_rule: Mapped[str] = mapped_column(String(16), default="POSITIONAL")
    allow_takeback: Mapped[bool] = mapped_column(Boolean, default=True)
    move_seconds: Mapped[int] = mapped_column(Integer, default=0)   # 每手限时秒数，0=不限时

    rank_id: Mapped[int] = mapped_column(Integer, default=DEFAULT_RANK_ID, index=True)
    ai_name: Mapped[str] = mapped_column(String(32), default="")
    is_promotion: Mapped[bool] = mapped_column(Boolean, default=False)   # 晋升战对局
    engine_name: Mapped[str] = mapped_column(String(32), default="")     # 实际使用的引擎

    status: Mapped[str] = mapped_column(String(16), default="playing", index=True)
    finished: Mapped[bool] = mapped_column(Boolean, default=False)
    finish_reason: Mapped[str] = mapped_column(String(32), default="")
    winner: Mapped[int] = mapped_column(Integer, default=0)              # 0=未定/和棋
    result_text: Mapped[str] = mapped_column(String(64), default="")
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    player_won: Mapped[bool] = mapped_column(Boolean, default=False)

    moves: Mapped[list] = mapped_column(JSON, default=list)              # Move.to_dict 列表
    analyses: Mapped[list] = mapped_column(JSON, default=list)           # 每手分析（胜率曲线数据源）
    sgf: Mapped[str] = mapped_column(Text, default="")
    takeback_count: Mapped[int] = mapped_column(Integer, default=0)

    # ---- 复盘 ----
    review_status: Mapped[str] = mapped_column(String(16), default="none")
    review_json: Mapped[dict] = mapped_column(JSON, default=dict)
    review_error: Mapped[str] = mapped_column(Text, default="")
    # 复盘进度（前端进度条用）：0~1 的完成度 + 阶段名 + 人读描述。
    # 复盘一盘 9 路棋要几十秒（等引擎预热时更久），只给 pending/done 两态
    # 看上去就像卡死了。
    review_progress: Mapped[float] = mapped_column(Float, default=0.0)
    review_stage: Mapped[str] = mapped_column(String(16), default="")
    review_detail: Mapped[str] = mapped_column(String(128), default="")
    accuracy: Mapped[float] = mapped_column(Float, default=0.0)          # 平均每手损失目数
    llm_used: Mapped[bool] = mapped_column(Boolean, default=False)

    user = relationship("User", back_populates="games")


class TsumegoProblem(Base):
    """死活题（包含对杀、吃子手筋、劫争等题型，见 kind）。

    lines 存的是完整答案（正解线 + 失败线与对方的最佳应对），**不能随题目
    一起下发**，否则前端打开开发者工具就能看答案；只在判定与「看答案」时返回。
    内置题的摆子与答案由 app/tsumego 的穷举搜索生成（不依赖人的记忆），
    导入题则来自 SGF（scripts/import_tsumego.py）。

    own / targets 是**非死活题的目标子**：吃子题的 targets = 要提掉的白子，
    对杀题的 own/targets = 两块棋各自的子，连络题的 own = 要连上的两个端点。
    它们既给前端画标记，也是服务端重建判据的原料（玩家落在变化线之外时要重新算结论）。

    difficulty 是 1~9，由 library.grade_difficulty 按**正解线手数**等可测量算出（不再手填）；
    tier 是它对应的五档人读标签（入门/初级/中级/高级/段位），入库是为了能直接筛选与分组。
    """
    __tablename__ = "tsumego_problems"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    title: Mapped[str] = mapped_column(String(96), default="")
    kind: Mapped[str] = mapped_column(String(16), default="life", index=True)  # life/ko/seki/race/capture/connect
    family: Mapped[str] = mapped_column(String(16), default="", index=True)   # 角上/边上/中腹/手筋
    goal: Mapped[str] = mapped_column(String(8), default="live", index=True)  # live/kill/ko_live/…
    difficulty: Mapped[int] = mapped_column(Integer, default=1, index=True)   # 1~9
    tier: Mapped[str] = mapped_column(String(8), default="入门", index=True)   # 入门/初级/中级/高级/段位
    size: Mapped[int] = mapped_column(Integer, default=9)
    to_move: Mapped[int] = mapped_column(Integer, default=1)     # 玩家执子色
    victim: Mapped[int] = mapped_column(Integer, default=1)      # 被判定的那一方（搜索的主角）
    setup: Mapped[list] = mapped_column(JSON, default=list)      # [[x, y, color], ...]
    space: Mapped[list] = mapped_column(JSON, default=list)      # 搜索范围 [[x, y], ...]
    targets: Mapped[list] = mapped_column(JSON, default=list)    # 要吃掉的子 / 对杀中对方的块
    own: Mapped[list] = mapped_column(JSON, default=list)        # 对杀中主角自己的块
    lines: Mapped[list] = mapped_column(JSON, default=list)      # 答案（不下发）
    hint: Mapped[str] = mapped_column(String(256), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(256), default="")  # 出处 / 许可说明
    builtin: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class TsumegoProgress(Base):
    """用户的死活题练习记录（做题次数 / 是否解出 / 是否看过答案）。"""
    __tablename__ = "tsumego_progress"
    __table_args__ = (UniqueConstraint("user_id", "problem_id", name="uq_tsumego_user_problem"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), index=True)
    problem_id: Mapped[str] = mapped_column(String(64), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    solved: Mapped[bool] = mapped_column(Boolean, default=False)
    seen_answer: Mapped[bool] = mapped_column(Boolean, default=False)
    last_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class RankEvent(Base):
    __tablename__ = "rank_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    kind: Mapped[str] = mapped_column(String(24))     # promote / demote / promo_start / promo_fail
    from_rank: Mapped[int] = mapped_column(Integer)
    to_rank: Mapped[int] = mapped_column(Integer)
    game_id: Mapped[str] = mapped_column(String(32), default="")
    detail: Mapped[str] = mapped_column(String(256), default="")
