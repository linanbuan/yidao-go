"""在同进程的一个后台线程里跑 uvicorn。

为什么用线程而不是子进程：双击即开、任务栏只有一个图标、不必把端口告诉任何人，
也不必再有一个"看得见终端"的宿主。

为什么仍然走 HTTP/WS 而不是直接函数调用：后端的 asyncio 事件循环里住着 KataGo
子进程、断链看门狗和复盘 worker，它们与 Qt 的事件循环**不能混用**。HTTP over
loopback 是这两个循环之间最笨也最稳的边界 —— 代价只是一个 127.0.0.1 上的随机端口。
"""
from __future__ import annotations

import copy
import logging
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


class BackendStartError(RuntimeError):
    """后端起不来。带上前端的日志尾部才有诊断价值。"""


def uvicorn_log_config(uvicorn_mod) -> dict | None:
    """给 uvicorn 一份**不碰控制台**的日志配置。

    现场（第 34 轮，玩家入口第一次被真跑）：双击 bat 走的是 `pythonw.exe`，那个进程里
    `sys.stdout` / `sys.stderr` 都是 `None`，而 uvicorn 默认配置里的
    `uvicorn.logging.DefaultFormatter` 构造时会调 `sys.stdout.isatty()` →
    AttributeError → `dictConfig` 抛 `ValueError: Unable to configure formatter 'default'`
    → **内嵌后端直接起不来**（窗口只剩一句「后端起不来」）。这条链在 pytest 与
    smoke 里全是 `python.exe`（有控制台），所以从来没被走过 —— 而它正是玩家双击的那条。

    两条一起给：`use_colors=False` 掐掉 `isatty()` 那条路径；**没有控制台时干脆整份
    配置都不给**（uvicorn 就不去配置日志了，交给后端自己的 `logging.basicConfig`），
    免得它再去碰 None 上的属性。
    """
    if sys.stdout is None or sys.stderr is None:
        return None
    cfg = copy.deepcopy(uvicorn_mod.config.LOGGING_CONFIG)
    for name in ("default", "access"):
        fmt = cfg.get("formatters", {}).get(name)
        if isinstance(fmt, dict):
            fmt["use_colors"] = False
    return cfg


class _NullCM:
    """既能 `with` 也能 `async with` 的空上下文管理器。

    为什么要同时支持两种：uvicorn 不公开这个钩子的形态，而且它换过 ——
    0.52.4 里是 `with self.capture_signals():`（同步），更早的版本是 `async with`。
    只实现一种就会在升级 uvicorn 时整块后端起不来，而不是一种能看出来的错。
    """

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ThreadedServer:
    """uvicorn.Server 的"能在子线程里跑"版本。

    uvicorn 的 serve() 会尝试为 SIGINT/SIGTERM 安装处理器：子线程里 `signal.signal`
    直接抛 ValueError，Windows 上 `loop.add_signal_handler` 又 NotImplemented。
    这里把信号捕获整段换成空操作 —— 信号交给主线程（也就是 Qt 与应用退出逻辑）处理。
    两个 override 分别对应 uvicorn 新旧两版的内部名，留一个不生效的那个是无害的。
    """

    def __new__(cls, config):
        import uvicorn

        class Impl(uvicorn.Server):
            def capture_signals(self):            # 实测 0.52.4：`with self.capture_signals()`
                return _NullCM()

            def install_signal_handlers(self):    # 旧版内部名
                pass

        return Impl(config)


def pick_free_port(host: str = "127.0.0.1") -> int:
    """让 OS 挑一个空闲端口。

    已知竞态：close 之后、uvicorn 真正 bind 之前，别的进程可能抢走这个端口。
    本机桌面场景概率极低，且调用方在启动失败时会换一个端口重试，够用。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


class BackendHost:
    """后端生命周期：start() 阻塞到 /api/health 就绪，stop() 走完整 lifespan 关停。"""

    def __init__(self, host: str = "127.0.0.1", log_level: str = "warning"):
        self.host = host
        self.log_level = log_level
        self.port: int | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self.error: str = ""

    # ---------------------------------------------------------------- 状态

    @property
    def base_url(self) -> str:
        if not self.port:
            raise BackendStartError("后端尚未启动")
        return f"http://{self.host}:{self.port}"

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive()
                    and self._server is not None and not self._server.should_exit)

    # ---------------------------------------------------------------- 启停

    def start(self, timeout: float = 60.0, attempts: int = 3) -> str:
        """启动并等待就绪，返回 base_url。

        等的是 `/api/health` 而不是"线程活着"：uvicorn 建套接字与 lifespan 启动
        之间有时间差，只看线程会把窗口开在一个还没有服务的进程上。
        引擎预热不在等待范围内（那是 `wait_until_ready` 的事，界面要先出来）。
        """
        last: str = ""
        for _ in range(max(1, attempts)):
            try:
                return self._start_once(timeout)
            except BackendStartError as exc:
                # 端口被抢（TOCTOU）或绑定失败：换一个端口重来
                last = str(exc)
                self._hard_reset()
                continue
        raise BackendStartError(last or "后端启动失败")

    def _start_once(self, timeout: float) -> str:
        import uvicorn

        from . import paths       # 延迟导入：paths.apply_env() 必须先于 app.* 被 import

        paths.apply_env()
        self.port = pick_free_port(self.host)
        config = uvicorn.Config(
            app="app.main:app",
            host=self.host,
            port=self.port,
            log_level=self.log_level,
            # 桌面端不需要 access log：每次落子都有好几条，全进 backend.log 会淹掉有用信息
            access_log=False,
            lifespan="on",
            # pythonw（无控制台）下必须绕开 uvicorn 默认的彩色格式化器，见 `uvicorn_log_config`
            log_config=uvicorn_log_config(uvicorn),
        )
        self._server = _ThreadedServer(config)
        self._thread = threading.Thread(target=self._serve, name="go-backend", daemon=True)
        self._thread.start()
        return self._wait_ready(timeout)

    def _serve(self) -> None:
        try:
            self._server.run()
        except Exception as exc:  # noqa: BLE001  线程里的异常没人接，必须自己记下来
            self.error = repr(exc)

    def _wait_ready(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        url = f"{self.base_url}/api/health"
        while time.monotonic() < deadline:
            if self.error:
                raise BackendStartError(f"后端线程异常：{self.error}")
            if not self._thread.is_alive() and self.error:
                raise BackendStartError(f"后端线程已退出：{self.error}")
            try:
                with urllib.request.urlopen(url, timeout=1.0) as resp:
                    if resp.status == 200:
                        return self.base_url
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.05)
        raise BackendStartError(f"后端在 {timeout:.0f} 秒内没有就绪（{url}）")

    def stop(self, timeout: float = 8.0) -> None:
        """优雅关停：让 uvicorn 退出主循环，从而跑完后端 lifespan 的 shutdown。

        这一步不是可选项 —— 只有走完整关停，KataGo 子进程才会被带走，
        日志里也才会出现 `Shutting down`（此前 15 次会话全是硬停，一次都没有）。
        """
        srv, th = self._server, self._thread
        if srv is not None:
            srv.should_exit = True
        if th is not None and th.is_alive():
            th.join(timeout)
        if th is not None and th.is_alive():
            # join 超时后**不能**直接清引用（审计 1.17）：daemon 线程会被 Qt 退出
            # 强杀，uvicorn lifespan 的 shutdown 没跑完 → katago.exe / pythonw.exe
            # 残留，下次启动双份进程同写一份数据目录。保留引用，允许调用方重试。
            self.error = f"后端在 {timeout:.0f} 秒内没有关停（线程仍在运行）"
            logger.warning("后端关停超时：%s", self.error)
            return
        self._thread = None
        self._server = None

    def restart(self, timeout: float = 60.0) -> str:
        """后端线程崩了之后的自恢复。端口会换，所以调用方要重新拿 base_url。"""
        self._hard_reset()
        return self.start(timeout)

    def _hard_reset(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            # 强重置前先确认旧线程真的走了，否则新旧两个 uvicorn 会同时写 SQLite
            try:
                self._thread.join(3.0)
            except RuntimeError:
                pass
            if self._thread.is_alive():
                logger.warning("旧后端线程仍未退出，继续尝试启动新实例（可能存在双写）")
        self._server = None
        self._thread = None
        self.port = None
        self.error = ""
