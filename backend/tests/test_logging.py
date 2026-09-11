"""日志装配：uvicorn 的 access/error 必须带时间戳并落进轮转文件（审计 §3.34）。

背景：`uvicorn` 与 `uvicorn.access` 两个 logger 都显式 `propagate=False`，
记录不会冒泡到 root —— 只把文件 handler 挂 root 的话，占运行日志 80% 的
access 行既不落盘、`uvicorn.error` 的启动/关闭消息也进不了文件。
"""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

import app.main as main

_TS = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _targets() -> list[logging.Logger]:
    return [logging.getLogger(), logging.getLogger("uvicorn"),
            logging.getLogger("uvicorn.access")]


def _strip_file_handlers() -> list[tuple[logging.Logger, logging.Handler]]:
    removed: list[tuple[logging.Logger, logging.Handler]] = []
    for t in _targets():
        for h in list(t.handlers):
            if isinstance(h, RotatingFileHandler):
                t.removeHandler(h)
                removed.append((t, h))
    return removed


def _restore(removed: list[tuple[logging.Logger, logging.Handler]]) -> None:
    for t, h in removed:
        t.addHandler(h)


def test_uvicorn_access_log_is_timestamped_and_persisted(tmp_path, monkeypatch):
    removed = _strip_file_handlers()
    try:
        monkeypatch.setattr(main, "DATA_DIR", tmp_path)
        main._configure_uvicorn_logging()
        main._attach_file_log()

        access = logging.getLogger("uvicorn.access")
        access.setLevel(logging.INFO)
        assert any(isinstance(h, RotatingFileHandler) for h in access.handlers), \
            "access 日志要直接挂轮转文件 handler（它 propagate=False，不会经 root）"

        access.info('127.0.0.1:5000 - "GET /api/games?token=eyJabc HTTP/1.1" 200')
        for h in access.handlers:
            h.flush()

        text = (tmp_path / "logs" / "backend.log").read_text(encoding="utf-8")
        lines = [ln for ln in text.splitlines() if "/api/games" in ln]
        assert lines, f"access 日志没落盘：{text!r}"
        assert _TS.match(lines[-1]), f"缺时间戳：{lines[-1]!r}"
        assert "token=***" in lines[-1], f"令牌没被脱敏：{lines[-1]!r}"
    finally:
        _strip_file_handlers()
        _restore(removed)


def test_file_handler_is_not_attached_twice(tmp_path, monkeypatch):
    """`_attach_file_log` 在 import 期与 lifespan 各调一次，必须幂等。"""
    removed = _strip_file_handlers()
    try:
        monkeypatch.setattr(main, "DATA_DIR", tmp_path)
        main._attach_file_log()
        main._attach_file_log()
        root = logging.getLogger()
        assert sum(isinstance(h, RotatingFileHandler) for h in root.handlers) == 1
    finally:
        _strip_file_handlers()
        _restore(removed)
