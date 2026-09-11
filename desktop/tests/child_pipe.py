"""被测试当「另一个进程」使的小工具：以本机进程的身份敲一次单实例管道。

用法：`python tests/child_pipe.py <pipe-name> <payload-hex> <result-file>`

为什么要真起一个子进程，而不是在测试进程里再开一个 `QLocalSocket`
（同进程的四条写法全都试过，都不稳 —— 症状是同一支测试忽绿忽红）：

  · 用 `waitFor*` 取连接或写：客户端一阻塞就把本线程唯一的循环占死，而服务端
    取连接（`nextPendingConnection`）必须靠这个循环 —— 两边自己把自己锁住，
    实测 `waitForBytesWritten` 返 False、对端 `wake_requests` 恒 0；
  · 不阻塞、只 pump 外层循环：能送到，但送达要撞进服务端 `_on_connection` 里
    那 200ms 的 `waitForReadyRead` 空档才算数，撞不上就 0 字节；
  · `flush()` 之后立刻 `disconnectFromServer()`：Windows 上写是异步的，这一句
    会把还没发出去的字节一起关掉（服务端永远读到 0 字节）；
  · 等 `bytesToWrite() == 0` 再断、并把 socket 握到断言做完（局部变量一出作用域
    就被引用计数销毁，句柄跟着关）：仍然撞上上面那条「循环空档」。

子进程各有各的循环、退出时句柄由内核冲掉，才是用户双击图标时真正发生的事。

载荷做成十六进制参数而不是字符串：要能发出「认不出的字节」才算测到白名单那一条。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtNetwork import QLocalSocket  # noqa: E402  要先补 sys.path


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: child_pipe.py <pipe-name> <payload-hex> <result-file>",
              file=sys.stderr)
        return 2
    name, payload_hex, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    sock = QLocalSocket()
    sock.connectToServer(name)
    connected = bool(sock.waitForConnected(3000))
    wrote = False
    if connected:
        sock.write(bytes.fromhex(payload_hex))
        sock.flush()
        wrote = bool(sock.waitForBytesWritten(2000))
        sock.disconnectFromServer()
        sock.waitForDisconnected(2000)
    # 先写临时名再改名：父进程看见文件在，内容就一定是完整的（不会读到半截）
    tmp = out.with_suffix(".part")
    tmp.write_text(f"connected={connected} wrote={wrote}", encoding="utf-8")
    tmp.replace(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
