"""API 依赖：数据库会话与当前用户。"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..security import user_id_from_token


def get_current_user(authorization: Optional[str] = Header(default=None),
                     db: Session = Depends(get_db)) -> User:
    user_id = user_id_from_token(authorization)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="未登录或登录已过期")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在")
    return user


def get_optional_user(authorization: Optional[str] = Header(default=None),
                      db: Session = Depends(get_db)) -> Optional[User]:
    user_id = user_id_from_token(authorization)
    if not user_id:
        return None
    return db.get(User, user_id)
