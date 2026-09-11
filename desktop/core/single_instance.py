"""单实例锁：同一个客户端只开一份，第二次启动只唤起已经开着的那个窗口。

为什么不用 TCP 端口或文件锁当身份：
  · 后端端口是 OS 现分配的（`backend_host.pick_free_port`），拿它当实例标识不稳定，
    而且会跟真正的服务端口抢；
  · 文件锁在进程被强杀之后没人负责清，下一次就永远起不来 —— 这类「锁没释放」的
    故障对用户是致命的（只能去手工删文件）。

`QLocalServer` 在 Windows 上就是**命名管道**：内核在最后一个句柄关闭时自己把名字
清掉，不留垃圾，也不占 TCP 端口、不会被防火墙拦。这正是计划写这条的理由。

诚实边界：抢前台这件事操作系统会拦。第二个实例只能**请求**已有窗口现身
（`raise_()` + `activateWindow()`），Windows 大概率只闪一下任务栏按钮而不给焦点 ——
那是系统的前台锁定策略，不是这里没接线。所以已有窗口一侧还补了一条气泡提示
（有托盘时），让「你已经在跑了」这件事至少有个看得见的回答。
"""
from __future__ import annotations

import getpass
import hashlib
import os
import re

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

#: 第二个实例发过来的全部内容。做成协议而不是「连上就算」：将来要区分
#: 「唤起窗口」与「打开某个棋谱」，靠的就是这个字节串。
REQUEST = b"activate"
#: 连与写的等待上限（毫秒）。**短**是刻意的：这一步失败也要能正常启动，
#: 拿一个卡住的实例去堵住第二个实例的启动是本末倒置。
CONNECT_MS = 1000
WRITE_MS = 500
#: 收那一边：`newConnection` 只说明「连上了」，**不保证字节已经到了**。
#: 直接 `readAll()` 在慢机器上会读到空，症状是「第二次启动没反应」。
#: 两个上限各 200ms：对端已经断开时 `waitForReadyRead` 会立刻返回 False，不会真等。
READ_MS = 200

SERVER_PREFIX = "Yidao.desktop"

#: 环境变量：把这扇门的管道名整个换掉。留这个口子有两个理由，都不是装饰：
#: ① 验收要**同机开两个真客户端进程**（验「二次启动只唤起已有窗口」），用默认名
#:    就会与开发者自己开着的那个客户端撞车 —— 测试以为自己唤起了窗口，
#:    实际唤走了人家那个；② 同一台机器上并存两份客户端（拿两个账号并排比棋）
#:    本就是一个说得通的用法，不该逼用户先关另一个。
PIPE_NAME_ENV = "GO_CLIENT_PIPE"


def default_name() -> str:
    """按「安装目录 + 当前用户」定管道名（可用 `PIPE_NAME_ENV` 直接指定）。

    带上目录是因为**同机两份 checkout 各开一份**是开发常态，互相拦住会让人
    以为程序坏了；带上用户是因为两个 Windows 用户各自的偏好与登录态完全独立
    （偏好存在各自的 AppData 里），拦在一起就是纯粹的故障。
    """
    env = os.environ.get(PIPE_NAME_ENV, "").strip()
    if env:
        return sanitize(env)
    from core import paths
    key = f"{str(paths.APP_DIR).lower()}|{getpass.getuser().lower()}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return f"{SERVER_PREFIX}.{digest}"


def sanitize(name: str) -> str:
    """把名字收成只含字母数字点横杠的样子（测试传中文名时不至于炸在系统层）。"""
    cleaned = re.sub(r"[^0-9A-Za-z._-]", "-", name or "")
    return cleaned[:60] or SERVER_PREFIX


class Gate(QObject):
    """本进程的实例锁。`activated` 是「又有第二次启动来了」的通知。"""

    activated = Signal()

    def __init__(self, name: str | None = None, parent=None):
        super().__init__(parent)
        self._name = sanitize(name or default_name())
        self._server: QLocalServer | None = None
        #: 这个名字是不是**本进程**占下的。只有占下过的人才许 `removeServer`：
        #: 第二个实例退出时 `aboutToQuit` 也会走 `release()`，跟着清名字就会把
        #: 先开那个实例的入口抹掉（症状：从此叫不动，而且看不出来）。
        self._owned = False
        #: 最后一次失败的原因（成功时要清空，不然旧错会跟着界面一路显示下去）
        self.last_error = ""
        #: 成功唤起过几个第二实例（验收要数这个，而不是「看起来窗口动了」）
        self.wake_requests = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def held(self) -> bool:
        return self._server is not None and self._server.isListening()

    # ---------------------------------------------------------------- 拿锁

    def acquire(self) -> bool:
        """True = 本实例是第一个，可以继续启动；False = 已经有别的实例在跑。

        返回 False 时**已经**给对方发过唤起请求，调用方应当直接退出。
        """
        if self.held:
            return True                      # 幂等：重复 acquire 不许把锁弄丢
        if self.notify_existing():
            return False
        if self._try_listen():
            self.last_error = ""
            return True
        self.last_error = self._server.errorString() if self._server is not None else ""
        self.last_error = self.last_error or "命名管道监听失败"
        if self._server is not None:
            self._server.close()
            self._server = None
        return True                          # 锁本身出问题时**放行**，见 docstring 下注

    def _try_listen(self) -> bool:
        """占住这个管道名。先直接试，不行就清掉残留的名字再试一次。

        单独一个方法是为了**可测**：「锁坏了也要放行」是这一层唯一一条
        「宁开两个窗口也不拦住启动」的分支，不抽出来就没法把 `listen` 弄失败，
        于是它永远没被跑过（而它恰恰是最不该错的那一条）。
        """
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_connection)
        if self._server.listen(self._name):
            self._owned = True
            return True
        # 名字还占着但没人接（上一个实例被强杀留下的残留）：清掉再试一次。
        QLocalServer.removeServer(self._name)
        if self._server.listen(self._name):
            self._owned = True
            return True
        return False

    # 拿不到锁宁可放行也不能拦住启动：这一层是"打磨"，不是安全边界。
    # 拦住的后果是双击图标什么都不发生 —— 比开两个窗口严重得多。

    def notify_existing(self) -> bool:
        """问一声「有人在跑吗」，有就让它把窗口拿出来。返回是否联系上了。"""
        sock = QLocalSocket()
        sock.connectToServer(self._name)
        if not sock.waitForConnected(CONNECT_MS):
            sock.abort()
            return False
        try:
            sock.write(REQUEST)
            sock.flush()
            sock.waitForBytesWritten(WRITE_MS)
        finally:
            sock.disconnectFromServer()
            # 枚举在这个 PySide6 版本里叫 `LocalSocketState`（写成 `SocketState` 会直接
            # AttributeError，而它在 `finally` 里 —— 第二个实例发完唤起请求正好炸在这儿）。
            if sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
                sock.waitForDisconnected(WRITE_MS)
        return True

    def is_running(self) -> bool:
        """只问有没有人在跑，不发唤起请求（启动器与测试要用这个口径）。"""
        sock = QLocalSocket()
        sock.connectToServer(self._name)
        ok = sock.waitForConnected(CONNECT_MS)
        sock.abort()
        return ok

    # ---------------------------------------------------------------- 收通知

    def _on_connection(self) -> None:
        while self._server is not None and self._server.hasPendingConnections():
            conn = self._server.nextPendingConnection()
            data = b""
            if conn.isValid():
                conn.waitForConnected(READ_MS)          # 已经连上时立即返回 True
                if not conn.bytesAvailable():
                    conn.waitForReadyRead(READ_MS)
                data = bytes(conn.readAll().data())
            conn.close()
            conn.deleteLater()
            # 认不出来的请求**不**唤起：管道名不带权限控制，本机任何进程都能写，
            # 所以「收到任何东西就抢前台」等于把焦点交给随便一个本机程序。
            if data.startswith(REQUEST):
                self.wake_requests += 1
                self.activated.emit()

    # ---------------------------------------------------------------- 放锁

    def release(self) -> None:
        """退出时主动放手（进程死了内核也会放手，这一条只是为了同进程重开）。"""
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None
        if not self._owned:
            return                                  # 这个名字不是我的，不能清
        self._owned = False
        QLocalServer.removeServer(self._name)
