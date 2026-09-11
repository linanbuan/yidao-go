"""FastAPI 入口：装配路由、启动引擎池与复盘 worker。

界面不在这一层：交付形态是 PySide6 桌面客户端（`desktop/`，把本服务内嵌在自己的进程里）。
旧网页端已于 2026-09-08 下线，`/ui` 静态托管与 SPA 回退一并移除。"""
from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from .api import auth, games, reviews, stats, system, tsumego, ws
from .config import DATA_DIR, settings
from .database import init_db
from .engine.pool import get_pool
from .review.worker import start_worker, stop_worker
from .tsumego.store import seed_builtin

_LOG_FMT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
logging.basicConfig(level=logging.INFO, format=_LOG_FMT)
logger = logging.getLogger("go.main")


class _RedactTokenFilter(logging.Filter):
    """把 URL 查询串里的 token 打成 ***。

    WebSocket 认证走 `?token=…`（浏览器/客户端 API 不支持 WS 自定义头），
    uvicorn 的 access log 会把整条 URL 原样打印 —— 实测 `token=eyJ…` 在
    backend.log 里出现过 57 次，等于把可用令牌写进磁盘（审计 1.3）。
    """

    _RE = re.compile(r"(token=)[^&\s\"']+", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = self._RE.sub(r"\1***", record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(
                    self._RE.sub(r"\1***", a) if isinstance(a, str) else a
                    for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {
                    k: (self._RE.sub(r"\1***", v) if isinstance(v, str) else v)
                    for k, v in record.args.items()}
        except Exception:   # noqa: BLE001
            pass
        return True


def _configure_uvicorn_logging() -> None:
    """给 uvicorn 的日志补时间戳、脱敏 token。

    此前 80% 的运行日志（uvicorn access）没有任何时间戳，故障时间线对不上。
    幂等：可重复调用（import 期一次，lifespan 里再兜一次）。
    """
    fmt = logging.Formatter(_LOG_FMT)
    redact = _RedactTokenFilter()
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            if not isinstance(h, RotatingFileHandler):
                h.setFormatter(fmt)
        lg.addFilter(redact)
    # access 有自己的 handler，别再被 root 打第二遍
    logging.getLogger("uvicorn.access").propagate = False


def _attach_file_log() -> None:
    """把轮转文件日志挂到 root 与 uvicorn 的 logger 上（幂等）。

    只挂 root 是不够的：`uvicorn` 与 `uvicorn.access` 都显式 `propagate=False`，
    它们的记录**不会**冒泡到 root —— 于是 access 日志既不落盘、`uvicorn.error`
    的启动/关闭消息也进不了文件，正是审计 §3.34 说的「80% 行无时间戳、从不轮转」。
    多进程混写同文件的问题由「单进程单 worker + 轮转」规避（本服务本就单进程）。

    只创建/安装一次：重复调用直接返回，避免同一个 logger 上挂两份 handler。
    """
    targets = [logging.getLogger(), logging.getLogger("uvicorn"),
               logging.getLogger("uvicorn.access")]
    if any(any(isinstance(h, RotatingFileHandler) for h in t.handlers) for t in targets):
        return
    try:
        log_dir = DATA_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_dir / "backend.log",
                                      maxBytes=5 * 1024 * 1024, backupCount=3,
                                      encoding="utf-8")
    except OSError as exc:   # 只读目录：退化为仅控制台
        logger.warning("无法启用文件日志（%s）", exc)
        return
    handler.setFormatter(logging.Formatter(_LOG_FMT))
    handler.addFilter(_RedactTokenFilter())
    for t in targets:
        t.addHandler(handler)


_configure_uvicorn_logging()
_attach_file_log()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # uvicorn 若在导入本模块之后又重配过日志（`dictConfig` 会替换 handler），
    # 这里再兜一次：两个函数都幂等，已应用就什么都不做。
    _configure_uvicorn_logging()
    _attach_file_log()
    init_db()
    logger.info("数据库就绪：%s", settings.database_url)
    try:
        seed_builtin()          # 死活题库（命中磁盘缓存时秒级；源码指纹变了才会重算，约两分钟）
    except Exception as exc:   # noqa: BLE001
        # 题库生成失败不应该拦住整个服务：对局与复盘照常可用
        logger.error("死活题库初始化失败（其余功能不受影响）: %s", exc)
    await get_pool().startup()
    await start_worker()
    # 这里**不**打监听地址：它由启动方决定。桌面端内嵌时端口是随机分配的、host 固定
    # 127.0.0.1，用 settings.host/port 拼出来就是一行“服务已启动：http://0.0.0.0:8000”
    # —— 看着能连其实连不上的假信息（实测出现过）。uvicorn 自己打的
    # “Uvicorn running on http://…” 才是真地址，不在这里重复一遍错的。
    logger.info("服务已启动")
    try:
        yield
    finally:
        await stop_worker()
        await get_pool().shutdown()
        logger.info("服务已停止")


app = FastAPI(
    title="弈道",
    description="KataGo 引擎 + 18级~九段等级体系 + 大模型复盘讲解",
    version="1.0.0",
    lifespan=lifespan,
    # `GO_DOCS_ENABLED=false` 时连 openapi.json 一起关掉（审计 1.22）：
    # 只关 /docs 而留着 openapi.json 等于文档仍然可读，只是没有那个页面。
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
)

app.add_middleware(
    CORSMiddleware,
    # 收窄到本机来源：桌面端与内嵌后端同源（127.0.0.1:<随机端口>），
    # `["*"]` 则意味着本机浏览器的任意网页都能读 127.0.0.1:8000 的响应。
    allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$",
    allow_origins=[],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 响应压缩。后端同时托管前端产物与题库接口，两边都有大块文本：
#   • 静态产物：echarts ~507KB、react ~164KB（gzip 后共 ~223KB → ~67KB）；
#   • 题库列表：154 题的 brief 约 159KB（JSON 结构重复多，压缩率极高）。
# 不压缩就是裸传。加在 CORS **之后** = 位于最外层，所以静态文件与错误响应也会被压缩，
# 而 CORS 头已在压缩前加好（GZip 只改 Content-Encoding / Content-Length，不动其他头）。
# minimum_size=1024：小响应压缩不划算，省下的字节抵不上 gzip 头与 CPU。
app.add_middleware(GZipMiddleware, minimum_size=1024)

app.include_router(auth.router)
app.include_router(games.router)
app.include_router(reviews.router)
app.include_router(stats.router)
app.include_router(tsumego.router)
app.include_router(system.router)
app.include_router(ws.router)


@app.get("/")
def root():
    body = {
        "service": "弈道",
        "engine": get_pool().active_engine,
    }
    if settings.docs_enabled:      # 关掉了就别在根路径上给它打广告
        body["docs"] = "/docs"
    return body


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
