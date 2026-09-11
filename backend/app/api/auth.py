"""认证与用户资料 API。"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..crypto import seal
from ..game.manager import get_hub
from ..models import RankEvent, User
from ..rank.defs import MAX_RANK_ID, MIN_RANK_ID, get_rank
from ..rank.logic import init_user_rank, progress_of
from ..security import create_token, hash_password, verify_password
from .deps import get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger("go.api.auth")

# ---- 登录失败限流（进程内存）----
# 单机单用户场景不需要 Redis 之类；目的只是把「无限爆破」变成「有成本」。
# 窗口内累计失败 MAX_FAILS 次即锁定，成功登录清零。
_FAILS: dict[str, list[float]] = defaultdict(list)
_FAILS_LOCK = threading.Lock()
MAX_FAILS = 8
FAIL_WINDOW = 300.0     # 秒


def _fail_key(username: str) -> str:
    return (username or "").strip().lower()


def _is_locked(key: str) -> bool:
    now = time.time()
    with _FAILS_LOCK:
        hits = [t for t in _FAILS[key] if now - t < FAIL_WINDOW]
        _FAILS[key] = hits
        return len(hits) >= MAX_FAILS


def _record_fail(key: str) -> None:
    with _FAILS_LOCK:
        _FAILS[key].append(time.time())


def _clear_fails(key: str) -> None:
    with _FAILS_LOCK:
        _FAILS.pop(key, None)


class RegisterIn(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=6, max_length=64)
    displayName: str = Field(default="", max_length=32)


class LoginIn(BaseModel):
    username: str
    password: str


class ProfileIn(BaseModel):
    displayName: Optional[str] = None
    hintMode: Optional[bool] = None
    demotionEnabled: Optional[bool] = None


class LLMConfigIn(BaseModel):
    baseUrl: Optional[str] = None
    apiKey: Optional[str] = None
    model: Optional[str] = None


def user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name or user.username,
        "createdAt": user.created_at.isoformat() if user.created_at else "",
        "hintMode": user.hint_mode,
        "demotionEnabled": user.demotion_enabled,
        "llmConfig": {
            "baseUrl": (user.llm_config or {}).get("baseUrl", ""),
            "model": (user.llm_config or {}).get("model", ""),
            # API Key 不回传明文，只告知是否已配置
            "hasApiKey": bool((user.llm_config or {}).get("apiKey")),
        },
        "progress": progress_of(user).to_dict(),
    }


@router.post("/register")
def register(body: RegisterIn, db: Session = Depends(get_db)):
    key = _fail_key(body.username)
    if _is_locked(key):
        raise HTTPException(429, "尝试过于频繁，请稍后再试")
    exists = db.scalar(select(User).where(User.username == body.username))
    if exists:
        _record_fail(key)
        raise HTTPException(400, "用户名已被占用")
    user = User(username=body.username, password_hash=hash_password(body.password),
                display_name=body.displayName or body.username)
    init_user_rank(user)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # 「先查后插」存在 TOCTOU：并发同名注册会撞唯一约束。以前是未捕获的
        # IntegrityError → 500；这里转成正常业务错误。
        db.rollback()
        raise HTTPException(400, "用户名已被占用")
    db.refresh(user)
    token = create_token(user.id, user.username)
    return {"token": token, "user": user_payload(user)}


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    key = _fail_key(body.username)
    if _is_locked(key):
        raise HTTPException(429, "登录失败次数过多，请 5 分钟后再试")
    user = db.scalar(select(User).where(User.username == body.username))
    if user is None or not verify_password(body.password, user.password_hash):
        _record_fail(key)
        raise HTTPException(401, "用户名或密码错误")
    _clear_fails(key)
    token = create_token(user.id, user.username)
    return {"token": token, "user": user_payload(user)}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return {"user": user_payload(user)}


@router.patch("/me")
def update_me(body: ProfileIn, user: User = Depends(get_current_user),
              db: Session = Depends(get_db)):
    if body.displayName is not None:
        user.display_name = body.displayName.strip()[:32]
    if body.hintMode is not None:
        user.hint_mode = body.hintMode
        # 「落子推荐」必须当场生效，不是下一局才生效：同步进该用户
        # 进行中的对局并广播（见 GameHub.set_hint_mode 的说明）。
        get_hub().set_hint_mode(user.id, body.hintMode)
    if body.demotionEnabled is not None:
        user.demotion_enabled = body.demotionEnabled
    db.commit()
    return {"user": user_payload(user)}


@router.put("/me/llm")
def update_llm(body: LLMConfigIn, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    cfg = dict(user.llm_config or {})
    if body.baseUrl is not None:
        cfg["baseUrl"] = body.baseUrl.strip().rstrip("/")
    if body.model is not None:
        cfg["model"] = body.model.strip()
    if body.apiKey is not None:
        # 传空字符串表示清除；否则覆盖。
        # 存进库的是**密文**（app/crypto.py，审计 1.22）：库文件可能被拷走或进备份，
        # 而 `users.llm_config` 是明文 JSON 列，API Key 不该在那里裸奔。
        if body.apiKey.strip():
            cfg["apiKey"] = seal(body.apiKey.strip())
        else:
            cfg.pop("apiKey", None)
    user.llm_config = cfg
    db.commit()
    return {"llmConfig": user_payload(user)["llmConfig"]}


@router.get("/me/timeline")
def timeline(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """等级流水（成长时间线）。"""
    rows = db.scalars(select(RankEvent).where(RankEvent.user_id == user.id)
                      .order_by(RankEvent.created_at.desc()).limit(100)).all()
    return {
        "items": [{
            "id": r.id,
            "kind": r.kind,
            "fromRank": r.from_rank,
            "fromRankName": get_rank(r.from_rank).name if r.from_rank else "",
            "toRank": r.to_rank,
            "toRankName": get_rank(r.to_rank).name if r.to_rank else "",
            "detail": r.detail,
            "gameId": r.game_id,
            "createdAt": r.created_at.isoformat() if r.created_at else "",
        } for r in rows],
        "rankRange": {"min": MIN_RANK_ID, "max": MAX_RANK_ID},
    }
