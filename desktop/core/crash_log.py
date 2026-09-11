"""崩溃日志：把没人接的异常写进文件。

为什么必须有这一层：桌面端将来跑在 `pythonw` / 打包的 exe 上，**没有控制台**。
而 PySide6 处理槽函数异常的方式是「调 `sys.excepthook`（默认就是往 stderr 打一条
traceback）然后继续跑」（实测 6.11.2：`QTimer.timeout` 的 Python 槽里抛 `RuntimeError`
→ 进程 rc=0 活着，只有 stderr 有东西；重写的 `paintEvent` 里抛同样走 `excepthook`，
外加 Qt 自己一句 `Error calling Python override of QWidget::paintEvent()`）。
也就是说：**没有这个文件，用户只会看见「某个按钮没反应」，什么证据都不留**，
而我们连「他那边到底炸没炸」都问不出来。

三条口径：

1. **异常不许被吞掉**：写完日志仍然把原文交给 stderr（有控制台时看得见，
   打包后写了个寂寞也不影响流程），并且**不改控制流** —— 该继续跑就继续跑。
   把 GUI 异常升级成退出是错的行为：一处画错的角标不该让人丢掉一整盘棋。
2. **必须限流**。实测 10ms 定时器里的同一个异常，200ms 就打了 23 条 ——
   本应用有 1 秒的落子钟与复盘轮询，崩一次的量级是「每秒几十条」，
   不限流就是几分钟写爆磁盘。做法：同一条签名（异常类型 + 消息 + 末帧）在
   `THROTTLE_SEC` 秒内只落一条，重复次数攒着，等签名换了或窗口过了补一句
   「上一条重复 N 次」，这样「崩得多频繁」这个最要紧的信息不丢。
3. **写日志这件事本身不许抛**。日志目录在只读介质上、盘满、正在关机 ——
   任何一种都不能让异常从 excepthook 里抛出去（那会让 PySide 走进二次异常）。
"""
from __future__ import annotations

import io
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from core import paths

LOG_NAME = "client.log"
#: 单个日志文件的上限与滚动后保留的尾部（字节）。
ROTATE_AT = 1_000_000
KEEP_TAIL = 200_000
#: 同一条异常在这个窗口内只记一次（见模块 docstring 第 2 条）。
THROTTLE_SEC = 60.0

_lock = threading.Lock()
_path: Path | None = None
_saved: dict[str, object] = {}
#: 当前被限流的那条签名、它的首次时间、以及又出现了多少次
_repeat_sig: str = ""
_repeat_at: float = 0.0
_repeat_n: int = 0
#: 本次运行里记过几条**不同**的异常（测试与「帮助 → 崩溃日志」都用得上）
recorded: int = 0
last_error: str = ""
#: `install(on_new=...)` 存下来的回调（每条**新**异常调一次）
_on_new = None


def log_path() -> Path:
    """日志文件路径。没装过就用默认目录（`install()` 会换成本机实际位置）。"""
    return _path if _path is not None else paths.LOGS_DIR / LOG_NAME


def install(log_dir=None, on_new=None) -> Path:
    """装上三个钩子，返回日志文件路径。重复调用是幂等的（钩子只装一层）。

    `log_dir` 给测试用：不传就落在 `paths.LOGS_DIR`，传了就把文件放到那个目录，
    这样验收不会往开发者真实的 `logs/` 里塞一堆假崩溃。
    `on_new(签名摘要)` 在**每条新异常**（未被限流）落盘后回调一次，
    界面拿它把「刚刚出过一次异常」告诉用户 —— 限流期间不回调，免得刷屏。
    """
    global _path, recorded, _repeat_sig, _repeat_at, _repeat_n, _on_new
    target = Path(log_dir) / LOG_NAME if log_dir else paths.LOGS_DIR / LOG_NAME
    if on_new is not None:
        # 不传就**保留**已有的回调：真实启动顺序是先 `install()` 后建窗口，
        # 而测试与验收脚本常常反过来（先开窗、再把日志改指到临时目录）——
        # 无条件的赋值会在那种顺序下把界面的通知接错掉，症状是「异常落盘了却没人说话」。
        _on_new = on_new
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:                       # 目录都建不出来：只剩 stderr 可走
        _err(f"崩溃日志目录建不出来（{target.parent}）：{exc!r}\n")
    _path = target
    if not _saved:
        _saved["excepthook"] = sys.excepthook
        _saved["unraisable"] = getattr(sys, "unraisablehook", None)
        _saved["threading"] = threading.excepthook
    sys.excepthook = _hook_excepthook
    if hasattr(sys, "unraisablehook"):           # 3.10 起有；老版本跳过而不是报错
        sys.unraisablehook = _hook_unraisable
    threading.excepthook = _hook_thread
    recorded = 0
    _repeat_sig, _repeat_at, _repeat_n = "", 0.0, 0
    _banner()
    return target


def set_notifier(fn) -> None:
    """装上/换掉「一条新异常」的回调（界面用）。与 `install(on_new=...)` 同一件事，
    分开一个入口是因为窗口往往比日志晚建（启动顺序不能要求）。"""
    global _on_new
    _on_new = fn


def uninstall() -> None:
    """把钩子还回去。测试收尾必须调，否则后面的用例会拿到一个写临时目录的钩子。"""
    global _path, _on_new
    if _saved.get("excepthook") is not None:
        sys.excepthook = _saved["excepthook"]
    if _saved.get("unraisable") is not None and hasattr(sys, "unraisablehook"):
        sys.unraisablehook = _saved["unraisable"]
    if _saved.get("threading") is not None:
        threading.excepthook = _saved["threading"]
    _saved.clear()
    _path = None
    _on_new = None


# ---------------------------------------------------------------- 三个钩子


def _hook_excepthook(etype, value, tb) -> None:
    """主线程里没人接的异常（含 Qt 从 C++ 回调进来的槽函数与虚拟重写）。"""
    text = "".join(traceback.format_exception(etype, value, tb))
    _record(f"未捕获异常（{threading.current_thread().name}）", text, value)
    if _saved.get("excepthook") is not None:
        try:
            _saved["excepthook"](etype, value, tb)     # 老规矩：继续往 stderr 打
        except Exception:                              # noqa: BLE001  旧钩子坏了也不能拦
            pass


def _hook_unraisable(unr) -> None:
    """`__del__`、ctypes 回调这类「抛不出去」的异常（PySide 某些路径也走这里）。

    参数对象的字段名在 3.12 / 3.13 之间动过（实测 3.12.10：只有
    `exc_type` / `exc_value` / `exc_traceback` / `err_msg` / `object`；3.13 改成
    `where` / `ctx`），所以除了异常本体之外全按 `getattr` 取 ——
    写死一个名字就会在升级 Python 时整条静默失效（不报错，只是不再落盘）。
    """
    value = getattr(unr, "exc_value", None)
    etype = getattr(unr, "exc_type", None) or type(value)
    text = "".join(traceback.format_exception(etype, value,
                                              getattr(unr, "exc_traceback", None)))
    bits = [str(s) for s in (getattr(unr, "err_msg", None),
                             getattr(unr, "where", None),
                             getattr(unr, "object", None),
                             getattr(unr, "ctx", None)) if s]
    head = "被忽略的异常" + (f"（{'；'.join(bits)[:200]}）" if bits else "")
    _record(head, text, value)


def _hook_thread(args) -> None:
    """子线程里的异常。后端线程、KataGo 看门狗、安装器都在这条路上。"""
    value = getattr(args, "exc_value", None)
    text = "".join(traceback.format_exception(
        getattr(args, "exc_type", type(value)), value,
        getattr(args, "exc_traceback", None)))
    th = getattr(args, "thread", None)
    name = getattr(th, "name", "?")
    _record(f"未捕获异常（线程 {name}）", text, value)


# ---------------------------------------------------------------- 落盘


def _signature(value, text: str) -> str:
    """一条异常的身份：类型 + 消息 + **末帧**（traceback 最后一行）。

    只用类型与消息会把「同一个 except 分支处理十个不同按钮」并成一条；
    末帧是抛出点，加上它就能区分开，同时同一个缺陷的重复仍然稳定命中。
    """
    frames = [ln for ln in text.splitlines() if ln.strip().startswith("File ")]
    return f"{type(value).__name__}:{_msg(value)}|{frames[-1] if frames else ''}"


def _msg(value) -> str:
    try:
        return str(value)[:300]
    except Exception:                              # noqa: BLE001  消息本身都可能不可格式化
        return "<消息不可读>"


def _record(head: str, text: str, value=None) -> bool:
    """记一条异常。返回 True 表示这次真的落盘了（False = 被限流）。"""
    global _repeat_sig, _repeat_at, _repeat_n, recorded, last_error
    sig = _signature(value, text)
    now = time.monotonic()
    with _lock:
        if sig == _repeat_sig and now - _repeat_at < THROTTLE_SEC:
            _repeat_n += 1
            return False
        _flush_repeats()                            # 换条了：先把重复数补上
        _repeat_sig, _repeat_at, _repeat_n = sig, now, 0
        recorded += 1
        last_error = f"{head}: {type(value).__name__}: {_msg(value)}"
        entry = f"[{_stamp()}] {head}\n{text.rstrip()}\n"
        cb = _on_new
    _write(entry)
    if cb is not None:
        try:
            cb(last_error)
        except Exception:                           # noqa: BLE001  通知界面失败不该影响记录
            pass
    return True


def _flush_repeats() -> None:
    """把攒下的重复次数写成一条统计（没有重复就什么都不做）。"""
    global _repeat_n, _repeat_sig
    if _repeat_n > 0 and _repeat_sig:
        n = _repeat_n
        _repeat_n = 0
        _write(f"[{_stamp()}] ↑ 上一条在 {THROTTLE_SEC:.0f} 秒内又出现了 {n} 次"
               f"（已合并，签名 {_repeat_sig[:120]}）\n")


def flush_repeats() -> None:
    """公开的门：把攒着的重复数立刻写出去（退出前调用，也供测试观察）。"""
    with _lock:
        _flush_repeats()


def _banner() -> None:
    _write(f"[{_stamp()}] 启动 · Python {sys.version.split()[0]} · "
           f"Qt {getattr(sys.modules.get('PySide6'), '__version__', '?')} · "
           f"frozen={bool(getattr(sys, 'frozen', False))} · "
           f"pid={os.getpid()}\n"
           f"    日志：{log_path()}\n"
           f"    数据：{paths.data_dir()}\n")


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write(message: str) -> None:
    """往同一个文件写普通一行（启动/关停这类流程信息，不算异常、不受限流）。"""
    _write(f"[{_stamp()}] {message.rstrip()}\n")


def _write(entry: str) -> None:
    try:
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(p)
        with p.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(entry)
    except Exception as exc:                        # noqa: BLE001  见模块 docstring 第 3 条
        _err(f"崩溃日志写不下去（{log_path()}）：{exc!r}\n{entry}")


def _rotate_if_needed(p: Path) -> None:
    try:
        if p.stat().st_size < ROTATE_AT:
            return
    except OSError:
        return
    try:
        with p.open("rb") as fh:
            fh.seek(-KEEP_TAIL, os.SEEK_END)
            tail = fh.read()
        head = f"[滚动] 更早的内容已丢弃，保留最后 {KEEP_TAIL} 字节\n".encode("utf-8")
        with p.open("wb") as fh:
            fh.write(head + tail)
    except OSError:
        pass                                        # 滚不动就照旧追加，别拦着记录


def _err(text: str) -> None:
    try:
        sys.stderr.write(text)
    except Exception:                               # noqa: BLE001  pythonw 下 stderr 可能是 None
        pass


def tail(limit: int = 6000) -> str:
    """日志尾部（给「帮助 → 打开崩溃日志」的对话框与失败消息用）。"""
    try:
        data = log_path().read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", "replace")


def summary() -> str:
    """一行总结，用在「刚刚出过一次异常」这种提示上。"""
    return last_error or ""


def capture(func) -> str:
    """把 `func()` 里会抛的异常抓成文本（不写日志）。给测试与诊断用。"""
    buf = io.StringIO()
    try:
        func()
    except BaseException:                            # noqa: BLE001  就是要把栈抓下来
        traceback.print_exc(file=buf)
    return buf.getvalue()
