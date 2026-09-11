"""动效总闸与三个共用动效。

**为什么要一个总闸**：动效是给眼睛的，验收（像素断言、截图）要的是终态。
`YIDAO_ANIM=0` 时全部动效退化成「直接到位」，`desktop/tests/conftest.py` 就是这么
钉的 —— 否则 `sample()` 采到的那一帧可能正好是棋子淡入到一半（半透明的子）。

**为什么动效必须先有便宜的重绘**：第 32 轮实测（19 路 640x640、盘上 180 子）
整盘重绘 22.25 ms —— 比一个 60fps 帧（16.7ms）还长。在那个数上叠动效，
等于把「卡」变成「更卡」。所以本轮的顺序是先做缓存（见 `GoBoard` 的三层缓存：
静态层 / 领地层 / 棋子精灵），量到单帧降到 1ms 级之后，再加动效。

时长都在 `DUR` 里：动效是"确认感"，超过 200ms 就从「顺滑」变成「拖沓」。
"""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QTimer
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

#: 环境变量总闸。没设 = 开；`0/false/off/no` = 关。
ENV = "YIDAO_ANIM"

#: 各类动效时长（毫秒）。`place` 是落子，`ring` 是最后一手的落定圈，
#: `hint` 是提示点淡入，`fade` 是面板出现，`scroll` 是跟着选中行走的滚动，
#: `bar` 是进度条走格。
DUR = {"place": 190, "ring": 360, "hint": 150, "fade": 140, "scroll": 150, "bar": 220}

_override: bool | None = None


def enabled() -> bool:
    """动效是否开启。测试与截图程序用 `YIDAO_ANIM=0` 钉成终态。"""
    if _override is not None:
        return _override
    raw = (os.environ.get(ENV) or "").strip().lower()
    return raw not in ("0", "false", "off", "no")


def set_enabled(value: bool | None) -> None:
    """进程内覆盖（`None` = 回到看环境变量）。给测试用。"""
    global _override
    _override = None if value is None else bool(value)


def duration(key: str) -> int:
    """某类动效的时长；总闸关掉时恒为 0（调用方据此直接跳到终态）。"""
    return DUR.get(key, 150) if enabled() else 0


def fade_in(widget: QWidget, key: str = "fade") -> None:
    """让一个刚出现的控件淡入。

    做成「控件自己拿得住的动画」而不是返回值：Qt 的动画对象被 GC 掉就静默不动，
    这个项目在 `Reply` 上已经吃过一次同类亏（`api.py` 里那段注释）。
    动画结束**必须摘掉 `QGraphicsOpacityEffect`** —— 留着它，这个控件以后每一帧
    都走离屏合成，正好是我们要减掉的那种开销。
    """
    ms = duration(key)
    if ms <= 0 or not widget.isVisible():
        _clear(widget)
        return
    eff = QGraphicsOpacityEffect(widget)
    eff.setOpacity(0.0)
    widget.setGraphicsEffect(eff)
    anim = QPropertyAnimation(eff, b"opacity", widget)
    anim.setDuration(ms)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    # 回调必须认出**自己**那条动画（审计 L9）：不绑的话，140ms 内第二次 `fade_in`
    # 会被上一条动画的 finished 顺手摘掉效果器 —— 第二次淡入静默失效。
    anim.finished.connect(lambda a=anim: _clear(widget, a))
    widget._fade_anim = anim            # 保活：见 docstring
    anim.start()


def _clear(widget: QWidget, anim=None) -> None:
    """摘掉淡入效果器与动画。

    `anim` 给的是「触发这次清理的那条动画」：只有当它仍是控件当前那条时才动手，
    否则就把更新的那条一起掐掉了（审计 L9）。
    """
    current = getattr(widget, "_fade_anim", None)
    if anim is not None and current is not anim:
        return
    widget.setGraphicsEffect(None)
    if current is not None:
        current.stop()
        widget._fade_anim = None


def animate_value(owner, name: str, to, key: str = "scroll") -> None:
    """把 `owner` 的某个数值属性（int/float）动画到 `to`。

    用途是进度条与滚动条：它们都在**控件内部**变化，不触发任何重排，
    所以是"零风险"的那类动效（不像 `QGraphicsOpacityEffect` 要走离屏合成）。
    """
    ms = duration(key)
    # 同一个属性上同时跑两条动画 = 每帧互相覆盖，滚动条/进度条会来回抖
    # （审计 M8：复盘 `_select_row` 以 30 次/秒触发 `animate_value`）。
    # 先停掉这一条属性的上一条，再决定要不要起新的。
    anims = getattr(owner, "_value_anims", None)
    if anims is None:
        anims = {}
        owner._value_anims = anims
    prev = anims.get(name)
    if prev is not None:
        prev.stop()
        anims[name] = None
    start = owner.property(name)
    if ms <= 0 or start is None or start == to:
        owner.setProperty(name, to)
        return
    anim = QPropertyAnimation(owner, name.encode(), owner)
    anim.setDuration(ms)
    anim.setStartValue(start)
    anim.setEndValue(to)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    anims[name] = anim
    owner._value_anim = anim            # 保活，同 `fade_in`
    anim.start()


def pulse_timer(owner, on_frame, ms: int = 16):
    """建一个 16ms 的帧定时器并挂到 `owner` 上（棋盘的两处动效共用）。"""
    timer = QTimer(owner)
    timer.setInterval(ms)
    timer.timeout.connect(on_frame)
    return timer


def now_ms() -> float:
    """单调时钟毫秒。动效用它算进度 —— 用挂钟时间的话，改系统时间会让动效卡住。"""
    return time.monotonic() * 1000.0
