"""对局 WebSocket 客户端。

协议对齐 `backend/app/api/ws.py`，行为对齐 `frontend/src/ws/socket.ts`：
  · 自动重连（最多 5 次，退避 600ms×n，上限 8s）—— 对局状态在后端落库，
    重连后服务端会推一份全量 `state`，客户端不需要自己补差；
  · 25 秒心跳 `{"action":"ping"}`，`pong` 不透给页面（它只用来保活）；
  · 连接还没 OPEN 时发的指令先排队，连上按序补发 —— 重连那一瞬玩家点棋盘，
    落子不能无声消失；队列上限 50，断网期间无限堆积没有意义。

`url` 用**可调用**传入：后端自重启会换端口、登录之后才有 token，两种情况都不该
要求重建这个对象。页面只认 `event` 与 `statusChanged` 两个信号。

QtWebSockets 的实测口径（6.11.2）：`connected/disconnected/textMessageReceived/
sendTextMessage/open/close/state/errorOccurred/closeCode` 都在；握手失败会先
`errorOccurred` 再 `disconnected`，所以重连逻辑统一挂在 `disconnected` 上。
构造器签名是 `QWebSocket(origin, version, parent)` —— **第一个位置参不是 parent**，
`QWebSocket(self)` 直接 TypeError（这一条是被测试撞出来的，不是看文档看出来的）。
"""
from __future__ import annotations

import json
import urllib.parse

from PySide6.QtCore import QObject, QUrl, QTimer, Signal
from PySide6.QtNetwork import QAbstractSocket
from PySide6.QtWebSockets import QWebSocket

#: 连接状态。与网页版 socket.ts 的四态同名，便于对照行为。
STATUSES = ("connecting", "open", "reconnecting", "closed")


def ws_url(base_url: str, path: str, token: str = "") -> str:
    """把 `http(s)://host:port` 换成 `ws(s)://` 并带上 token。

    令牌走 query 而不是 header：`QWebSocket` 没有简单的自定义头入口，而后端
    `ws.py` 的签名就是 `token: str = ""`（与网页版 `wsUrl()` 同口径）。
    """
    url = base_url.rstrip("/") + path
    for http, ws in (("https://", "wss://"), ("http://", "ws://")):
        if url.startswith(http):
            url = ws + url[len(http):]
            break
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}token={urllib.parse.quote(token, safe='')}"


class GameSocket(QObject):
    """一条对局连接。`event(dict)` 在**主线程**触发，可直接刷界面。"""

    event = Signal(dict)              # 已解析的服务端事件（pong 已滤掉）
    statusChanged = Signal(str)       # connecting / open / reconnecting / closed

    MAX_RETRIES = 5
    PENDING_LIMIT = 50
    HEARTBEAT_MS = 25_000

    def __init__(self, url_provider, parent=None):
        super().__init__(parent)
        self._url = url_provider
        self._retries = 0
        self._closed_by_user = True
        self._pending: list[dict] = []
        self.bad_frames = 0           # 解析不了的帧数：协议漂移时这里是唯一线索
        self.pong_count = 0           # 收到过几次 pong：心跳活着（不透给页面，得有地方记）
        self.last_error = ""

        # 必须写关键字：`QWebSocket(origin, version, parent)` 的第一个位置参是 origin，
        # `QWebSocket(self)` 会把 QObject 当字符串传 → TypeError（实测 6.11.2）。
        self._ws = QWebSocket(parent=self)
        self._ws.connected.connect(self._on_open)
        self._ws.disconnected.connect(self._on_close)
        self._ws.textMessageReceived.connect(self._on_text)
        self._ws.errorOccurred.connect(self._on_error)

        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(self.HEARTBEAT_MS)
        self._heartbeat.timeout.connect(lambda: self._raw_send({"action": "ping"}))

        # 重连定时器必须是**成员**：`QTimer.singleShot` 排出去就取消不掉了。
        # 后果（审计 1.18）：网络抖一下排了 600ms 重连，紧接着后端自重启又触发
        # `reconnect()`（那条路径会 close 再 open）→ 排着的那一次稍后仍然开火，
        # 在一个已经连好的 socket 上再 open 一次，徽章被打回「连接中…」、
        # 服务端再推一份全量 state。
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._open)

    # ---------------------------------------------------------------- 生命周期

    def connect_to_game(self) -> None:
        """开始（或重新开始）一条连接。重复调用是幂等的：先拆掉旧的。"""
        if self._ws.state() != QAbstractSocket.SocketState.UnconnectedState:
            self._ws.abort()
        self._closed_by_user = False
        self._open()

    def close(self) -> None:
        """用户/页面主动断开：不再重连，并丢掉排队中的指令。"""
        self._closed_by_user = True
        self._pending.clear()
        self._heartbeat.stop()
        self._retry_timer.stop()
        if self._ws.state() != QAbstractSocket.SocketState.UnconnectedState:
            self._ws.close()

    def reconnect(self) -> None:
        """后端换了端口（自重启）时用：当作一次全新的连接，重试预算清零。"""
        self._retries = 0
        self.close()
        # close() 会置 closed_by_user，这里必须重新打开意图，否则下一句就白叫了
        self.connect_to_game()

    @property
    def is_open(self) -> bool:
        return self._ws.state() == QAbstractSocket.SocketState.ConnectedState

    @property
    def status(self) -> str:
        if self.is_open:
            return "open"
        if self._closed_by_user:
            return "closed"
        return "reconnecting" if self._retries else "connecting"

    # ---------------------------------------------------------------- 收发

    def send(self, msg: dict) -> None:
        """发一条指令。没连上就排队（上限 PENDING_LIMIT）。"""
        if self._closed_by_user:
            # 页面已经切走/主动断开：再收指令只会攒出一堆永远发不出去的动作
            self._pending.clear()
            return
        if self.is_open:
            self._raw_send(msg)
        elif len(self._pending) < self.PENDING_LIMIT:
            self._pending.append(msg)

    def _raw_send(self, msg: dict) -> None:
        self._ws.sendTextMessage(json.dumps(msg, ensure_ascii=False))

    # ---------------------------------------------------------------- 内部

    def _open(self) -> None:
        if self._closed_by_user:
            # 迟到的重连定时器（关页/换盘之后才到点）：不许再把连接开起来
            return
        url = self._url() if callable(self._url) else self._url
        self.statusChanged.emit("reconnecting" if self._retries else "connecting")
        self._ws.open(QUrl(url))

    def _on_open(self) -> None:
        self._retries = 0
        self.last_error = ""
        self._retry_timer.stop()      # 已经连上，撤掉待发的重连
        self.statusChanged.emit("open")
        self._heartbeat.start()
        queued, self._pending = self._pending, []
        for msg in queued:
            self._raw_send(msg)

    def _on_close(self) -> None:
        self._heartbeat.stop()
        if self._closed_by_user:
            self.statusChanged.emit("closed")
            return
        if self._retries < self.MAX_RETRIES:
            self._retries += 1
            self.statusChanged.emit("reconnecting")
            delay = min(8000, 600 * self._retries)
            self._retry_timer.start(delay)
        else:
            self.statusChanged.emit("closed")

    def _on_error(self, _err, text: str) -> None:
        """只记账。真正的重连由 disconnected 统一负责（握手失败两个信号都会来）。"""
        self.last_error = text or self._ws.errorString()

    def _on_text(self, message: str) -> None:
        try:
            data = json.loads(message)
        except (ValueError, TypeError):
            self.bad_frames += 1
            return
        if not isinstance(data, dict):
            self.bad_frames += 1
            return
        if data.get("type") == "pong":
            # 只记账不透传：页面见到 pong 只会多一个它必须忽略的分支
            self.pong_count += 1
            return
        self.event.emit(data)
