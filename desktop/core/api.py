"""REST 客户端：所有请求都在 QThreadPool 里跑，结果用信号送回主线程。

为什么不直接在 GUI 线程里 urllib：题库列表 154 题的 brief 约 159KB、复盘 report
更大，本地回环虽然快（毫秒级），但**任何一次磁盘刷或 GC 停顿都会冻住窗口**。
网页版没有这个问题（fetch 天然异步），原生端就得自己把 I/O 挪出 GUI 线程。

为什么用 stdlib 而不是 httpx/requests：桌面端依赖越少，将来打包越省事；
这里只需要"发一个 JSON、拿一个 JSON"，urllib 完全够。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, QEventLoop, Signal


class ApiError(RuntimeError):
    """一次失败的请求。status=0 表示连上了但没有 HTTP 状态（超时/拒绝连接）。"""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ 纯函数层
# 不依赖 Qt，可单独单元测试；错误文案与 frontend/src/api/client.ts 保持一致口径。

def err_text(err) -> str:
    """把 `ApiError` 拉成一行可读中文。`ApiError.args[0]` 就是它的 message，
    直接 `str(err)` 也一样，但别的异常对象不一定。

    为什么在 `core.api`：这个转换过去在 `review.py`、`game.py` 各写了一份（且
    `None` 的口径还不一样：一份给 `""`、一份给 `"None"`），大厅要写第三份时决定
    收到这里来 —— 它跟着 `ApiError` 走，就不该比 `ApiError` 更远离调用方。
    取 `None -> ""` 那份：调用点都是 `f"某某失败：{err_text(err)}"`，那里宁可
    少一个字也不要一个骗人的 `None`。
    """
    args = getattr(err, "args", ())
    if args and isinstance(args[0], str) and args[0]:
        return args[0]
    return str(err) if err is not None else ""


def http_json(method: str, url: str, body=None, token: str = "", timeout: float = 20.0):
    raw = _urlopen(method, url, body, token, "application/json", timeout)
    if raw is None:
        return None
    text = raw.decode("utf-8")
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def http_text(method: str, url: str, token: str = "", timeout: float = 20.0) -> str:
    """导出类接口（SGF / Markdown 报告）要的是原文，不能被 JSON 解析一遍。"""
    raw = _urlopen(method, url, None, token, "text/plain", timeout)
    return (raw or b"").decode("utf-8")


def _urlopen(method: str, url: str, body, token: str, accept: str, timeout: float):
    data = None
    headers = {"Accept": accept}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 204:
                return None
            return resp.read()
    except urllib.error.HTTPError as exc:            # 有状态码的业务失败
        raise ApiError(_detail(exc), exc.code) from exc
    except urllib.error.URLError as exc:             # 连不上 / DNS / 拒绝
        raise ApiError(f"无法连接本地服务：{exc.reason}", 0) from exc
    except (TimeoutError, OSError) as exc:           # 超时与其他 socket 错误
        raise ApiError(f"请求超时或连接中断：{exc}", 0) from exc


def _detail(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except Exception:  # noqa: BLE001  非 JSON 错误页（如 502 的 HTML）
        return f"请求失败（HTTP {exc.code}）"
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail:
            return detail
        if detail:                       # FastAPI 校验错误的是 list[dict]，摊平成可读一行
            return _flatten_validation(detail)
    if isinstance(payload, str) and payload:
        return payload
    return f"请求失败（HTTP {exc.code}）"


def _flatten_validation(detail) -> str:
    """把 422 的 `[{loc, msg}, ...]` 收成一行中文可懂的提示。"""
    try:
        items = detail if isinstance(detail, list) else detail.get("detail", [])
        parts = []
        for it in items or []:
            loc = ".".join(str(s) for s in it.get("loc", []) if s != "body")
            parts.append(f"{loc}: {it.get('msg', '')}".strip(": "))
        return "；".join(p for p in parts if p) or "请求参数不合法"
    except Exception:  # noqa: BLE001
        return "请求参数不合法"


# ------------------------------------------------------------------ 异步层

def build_url(base: str, path: str, query: dict | None = None) -> str:
    url = base.rstrip("/") + path if path.startswith("/") else f"{base.rstrip('/')}/{path}"
    if query:
        clean = {k: v for k, v in query.items() if v is not None}
        if clean:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean, doseq=True)
    return url


class Reply(QObject):
    """一次请求的凭据。`finished(data, error)` 一定在**主线程**触发。

    接这个信号只有一种写法：`reply.finished.connect(self._on_loaded)` —— 槽必须是
    一个活得够久的 QObject 的**绑定方法**。实测（PySide6 6.11.2 / Windows）：
    同一个 Reply，槽是绑定方法时 8/8 送达，槽是 lambda 时 **0/8**（哪怕外层
    还持着这个 lambda 的强引用）—— 不报错、不崩溃，就是静默收不到。
    图省事写 `client.get(...).finished.connect(lambda d, e: ...)` 会得一个白板页面。
    """

    finished = Signal(object, object)

    def __init__(self):
        super().__init__()
        self._done = False
        self._result = (None, None)
        self.finished.connect(self._on_finished)

    def _emit(self, data, error) -> None:
        self.finished.emit(data, error)

    def _on_finished(self, data, error) -> None:
        self._result = (data, error)
        self._done = True

    @property
    def done(self) -> bool:
        return self._done

    def result(self):
        """(data, error)。未完成时返回 (None, None)。"""
        return self._result

    def result_or_raise(self):
        data, error = self._result
        if error is not None:
            raise error if isinstance(error, ApiError) else ApiError(str(error))
        return data

    def wait(self, timeout_ms: int = 20000):
        """阻塞当前线程直到本请求完成，用于启动路径与测试。

        转的是**事件循环**而不是干等：等待期间窗口仍能重绘、不假死。
        超时不算错误 —— 返回 (None, None) 由调用方决定怎么办（避免把慢网络变成异常）。
        """
        if self._done:
            return self._result
        loop = QEventLoop()
        self.finished.connect(loop.quit)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(timeout_ms)
        if not self._done:
            loop.exec()
        timer.stop()
        return self._result


class ApiClient(QObject):
    """REST 入口。`base_url` 用可调用传入，这样后端自重启换了端口也不用重建客户端。"""

    unauthorized = Signal()          # 任一请求带回 401：令牌没了，外壳该退回登录页

    def __init__(self, base_url_provider, token_provider=None, parent=None):
        super().__init__(parent)
        self._base = base_url_provider
        self._token = token_provider or (lambda: "")
        self._pool = QThreadPool.globalInstance()
        # 在途请求的引用。详见 _track()：没有这张表，工作线程回话时 Reply 可能已被析构。
        self._inflight: set[Reply] = set()

    # ------------------------------------------------------------ 属性

    @property
    def base_url(self) -> str:
        """当前后端的根地址。对局页要用它拼 ws URL（每次现取，端口变了也对）。"""
        return self._base()

    @property
    def token(self) -> str:
        return self._token()

    # ------------------------------------------------------------ 内部

    def _track(self, reply: "Reply") -> "Reply":
        """主线程必须持有在途 Reply 的引用。

        Qt 的对象归**创建它的线程**所有：`Reply` 在主线程建，工作线程 emit 时
        会把调用排回主线程队列。假设调用方只在局部变量里拿一下（`api.get(...).wait()`
        或连完信号就返回），Python 会把它回收掉，工作线程手里就只剩一个悬空指针 ——
        症状是偶发、栈不完整的崩溃，最难查。所以由客户端兼做这个保活。
        """
        self._inflight.add(reply)
        # 顺序有意：`_forget` 会放掉最后一个强引用，所以它必须排最后 ——
        # 先丢引用再跑后面的槽，等于在 emit 途中拆自己的台。
        reply.finished.connect(self._watch_auth)  # 401 要能被整个应用看见，不只是当前这页
        reply.finished.connect(self._forget)      # 不传 lambda：避开 reply↔闭包 的引用环
        return reply

    def _watch_auth(self, _data, error) -> None:
        """只把 401 往上抛。其余错误留给页面自己显示 —— 全局弹一条
        「读取失败」对 404/校验错只会是噪音。"""
        if isinstance(error, ApiError) and error.status == 401:
            self.unauthorized.emit()

    def _forget(self, *_a) -> None:
        sender = self.sender()
        if sender is not None:
            self._inflight.discard(sender)

    # ------------------------------------------------------------ 异步接口

    def request(self, method: str, path: str, body=None, query=None,
                timeout: float = 20.0) -> Reply:
        reply = self._track(Reply())
        job = _Job(self._task(method, path, body, query, timeout), reply)
        self._pool.start(job)
        return reply

    def get(self, path: str, query=None, timeout: float = 20.0) -> Reply:
        return self.request("GET", path, query=query, timeout=timeout)

    def post(self, path: str, body=None, query=None, timeout: float = 20.0) -> Reply:
        return self.request("POST", path, body=body, query=query, timeout=timeout)

    def patch(self, path: str, body=None, timeout: float = 20.0) -> Reply:
        return self.request("PATCH", path, body=body, timeout=timeout)

    def put(self, path: str, body=None, timeout: float = 20.0) -> Reply:
        return self.request("PUT", path, body=body, timeout=timeout)

    def delete(self, path: str, timeout: float = 20.0) -> Reply:
        return self.request("DELETE", path, timeout=timeout)

    def get_text(self, path: str, timeout: float = 30.0) -> Reply:
        reply = self._track(Reply())
        url = build_url(self._base(), path)
        token = self._token()
        job = _Job(lambda: _safe(http_text, "GET", url, token, timeout), reply)
        self._pool.start(job)
        return reply

    # ------------------------------------------------------------ 同步接口

    def request_sync(self, method: str, path: str, body=None, query=None,
                     timeout: float = 20.0):
        """阻塞拿结果。只给启动流程与测试用 —— 界面代码一律走异步接口。"""
        return self._task(method, path, body, query, timeout)()

    # ------------------------------------------------------------ 内部

    def _task(self, method: str, path: str, body, query, timeout):
        url = build_url(self._base(), path, query)
        token = self._token()

        def run():
            return _safe(http_json, method, url, body, token, timeout)
        return run


def _safe(fn, *args):
    """把异常折进返回值，好让 worker 线程不抛出（线程里抛异常没人接）。"""
    try:
        return (fn(*args), None)
    except ApiError as exc:
        return (None, exc)
    except Exception as exc:  # noqa: BLE001
        return (None, ApiError(f"客户端内部错误：{exc!r}"))


class _Job(QRunnable):
    def __init__(self, fn, reply: Reply):
        super().__init__()
        self._fn = fn
        self._reply = reply
        self.setAutoDelete(True)

    def run(self) -> None:                       # 线程池的工作线程
        data, error = self._fn()
        self._reply._emit(data, error)           # 跨线程 emit → 自动排队到主线程
