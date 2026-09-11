"""截图与像素断言工具 —— 「我自己验收」靠的就是这一层。

为什么要像素断言而不是只看截图：截图给我看是一次性的、不可回归的；
像素断言是每次跑测试都会执行的。两者都要 —— 断言保证不回归，看图发现断言没想到的问题。

坐标口径（**最容易错的一处**）：`widget.grab()` 出来的 QImage 是**设备像素**，
逻辑坐标 (lx, ly) 要乘 DPR 才能在图上取样，`sample()` 已经把这件事封装掉。

但别把 DPR 当成一个常数写进测试（这一句原来写的是「offscreen 下恒为 1.0」，
而它当时就已是假的：平台没人钉住，那几百条「绿」其实跑在开发者那块真屏上，
DPR 1.5 —— 见 `conftest` 顶部）。现在夹具把平台钉成 offscreen，本机量到 1.0，
可这不是理由去写死它。两条约束：
  · 取点的坐标一律走 `sample()`/`devicePixelRatioF()`，不许自己乘；
  · 凡是拿「图像素个数」当判据的，预算要按**最坏那一档**（DPR 1.0）算 ——
    尺寸类的期望值则根本不该出现（`restoreGeometry` 会把窗口夹进可用屏幕，
    800x800 的虚拟屏与 1707x960 的真屏给出不同答案，这一类断言在
    `test_global_flow.py` 里靠「存最小尺寸」两边都对）。
还有一句：差一圈抗锯齿就定生死的预算本来就不该写（胜率曲线那枚 7px
宽的散点就是这么红过一次）。
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget

from core import paths


def katago_requested() -> bool:
    """这一趟跑测是否要求 **KataGo 就绪**（显式 `GO_KATAGO_ENABLED=true`）。

    计划对 P2 的口径是「KataGo 就绪 / 未就绪两种都要跑」。做法不是另写一份测试，
    而是**同一个文件换个环境变量再跑一遍**（`GO_KATAGO_ENABLED=true` 起一个独立的
    pytest 进程）—— 所以凡是与引擎有关的断言都得看这个开关而不是写死启发式，
    否则那一支会在第一条断言上就红，红得看不出是「引擎不对」还是「测试不对」。
    """
    return os.environ.get("GO_KATAGO_ENABLED", "false").strip().lower() \
        in ("1", "true", "yes", "on")


def wait_engine_active(url: str, token: str, want: str = "katago",
                       timeout: float = 300.0) -> tuple[dict, str]:
    """轮询 `/api/system/status` 直到 `engine.active == want`。

    返回 `(最后一次看到的 engine 段, 最后一次请求异常)`：超时不当异常抛，
    因为“到底没起来”这件事得由断言去定性（才能把死因、stderr 路径拼进失败消息）。

    为什么要等而不是直接断：`pool.startup()` 只把预热丢进后台任务就返回
    （`/api/health` 得尽早应答，否则启动器的就绪探测直接超时），所以
    **host 就绪 ≠ 引擎就绪**：没切换完之前查询会合法地落在启发式引擎上。
    不等就开局，KataGo 那一支拿到的 `active="heuristic"` 是真值而不是 bug，
    断言会红在一个根本没坏的地方。
    """
    import time

    from core import api as A

    deadline = time.monotonic() + timeout
    eng: dict = {}
    last_err = ""
    while time.monotonic() < deadline:
        try:
            data = A.http_json("GET", url, token=token)
            eng = (data or {}).get("engine") or {}
            last_err = ""
            if eng.get("active") == want:
                return eng, last_err
        except Exception as exc:   # noqa: BLE001  服务未就绪/瞬时错都继续轮询到超时
            last_err = repr(exc)
        time.sleep(1.0)
    return eng, last_err


def _require_glyphs() -> None:
    """截图前先问一句：这个进程里有字体吗。

    offscreen 平台不注册 Windows 字体库（`QFontDatabase.families()` 为空），那种情况下
    每个字都画成 .notdef 方框 —— 图看着是满的，其实一个字都读不出来，
    “逐张读关键帧”当场变成假验收。夹具（`conftest.install_cjk_font`）负责装字体，
    这里负责在装不上时报错，而不是静默出一堆豆腐。"""
    from PySide6.QtGui import QFontDatabase

    fams = QFontDatabase.families()
    assert fams, ("测试进程里没有任何字体：截图会是满屏豆腐块，读不出一个字。"
                  "见 conftest.install_cjk_font 与它的 FONT_CANDIDATES。")


def snap(widget: QWidget, name: str) -> Path:
    """把控件画成 PNG 落到 artifacts/，返回路径（我随后逐张看）。"""
    _require_glyphs()
    d = paths.ARTIFACTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    widget.show()
    widget.raise_()
    _process()
    img = widget.grab().toImage()
    out = d / f"{name}.png"
    img.save(str(out))
    return out


def _process(times: int = 3) -> None:
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    for _ in range(times):
        app.processEvents()


def grab_image(widget: QWidget):
    """返回 **QImage**。

    `widget.grab()` 给的是 QPixmap，它没有 `pixelColor()`（只有 save()/toImage()）；
    取像素必须先转 QImage。这一层统一转好，测试里就不用各自记。
    """
    widget.show()
    _process()
    return widget.grab().toImage()


def sample(widget: QWidget, lx: float, ly: float) -> QColor:
    """按**逻辑坐标**取一个像素的颜色。"""
    img = grab_image(widget)
    dpr = float(widget.devicePixelRatioF())
    x = min(img.width() - 1, max(0, int(round(lx * dpr))))
    y = min(img.height() - 1, max(0, int(round(ly * dpr))))
    return img.pixelColor(x, y)


def sample_many(widget: QWidget, points) -> list[QColor]:
    """一次抓图取多个点：grab() 不便宜，别在循环里反复抓。"""
    img = grab_image(widget)
    dpr = float(widget.devicePixelRatioF())
    out = []
    for lx, ly in points:
        x = min(img.width() - 1, max(0, int(round(lx * dpr))))
        y = min(img.height() - 1, max(0, int(round(ly * dpr))))
        out.append(img.pixelColor(x, y))
    return out


def wait(qapp, predicate, timeout: float = 20.0, interval: float = 0.01) -> bool:
    """转主线程事件循环直到 `predicate()` 为真，超时返回 False。

    不能写成 `for _ in range(600): qapp.processEvents()`：那样几毫秒就把 600 轮
    跑完了，而工作线程/对端的回包还在路上 —— 看上去就是“信号没触发”。
    真实客户端没这个问题（主循环本来就在转），测试里必须自己给它时间。
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(interval)
    qapp.processEvents()
    return bool(predicate())


def settle(qapp, seconds: float = 1.0) -> None:
    """把事件循环空转一段墙钟时间。

    用途只有一个：**给「不该发生的第二份送达」留足时间**。`wait(pred)` 一真就返回，
    所以“过了这一刻还只响了一声”这类断言紧跟在它后面做就是抛硬币（第二份到底
    在断言前还是后落地全看调度）；先把这一秒转完再数，才是必然。
    """
    wait(qapp, lambda: False, timeout=seconds)


def luma(c: QColor) -> int:
    return int(0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue())


def color_distance(a: QColor, b: QColor) -> int:
    return abs(a.red() - b.red()) + abs(a.green() - b.green()) + abs(a.blue() - b.blue())


def is_wood(c: QColor) -> bool:
    """棋盘底色：暖色（R>G>B）且偏亮。用来判断「这个点没有子也没有线」。"""
    return luma(c) > 150 and c.red() > c.blue() + 20


def blank_ratio(widget: QWidget) -> float:
    """非背景像素占比。截图验收的第一道闸：全白/全空的图不该通过。"""
    img = grab_image(widget)
    bg = QColor("#f6f4ef")
    same = 0
    step = max(1, img.width() // 120)
    total = 0
    for y in range(0, img.height(), step):
        for x in range(0, img.width(), step):
            total += 1
            if color_distance(img.pixelColor(x, y), bg) < 12:
                same += 1
    return 1.0 - (same / max(1, total))


def _count_diff(a, b) -> int:
    """两张同尺寸 QImage 有多少个像素不同；尺寸不同当异常报出来（那是对齐没做好）。"""
    if a.size() != b.size():
        raise AssertionError(f"两张图尺寸不同：{a.width()}x{a.height()} vs "
                             f"{b.width()}x{b.height()}（同一个控件两态不该变尺寸）")
    return sum(1 for y in range(a.height()) for x in range(a.width())
               if a.pixelColor(x, y) != b.pixelColor(x, y))


def toggle_state_diff(box: QWidget) -> tuple[int, int]:
    """把同一个可勾选控件按「开 / 关」各画一次，返回 `(两态不同像素数, 同态自比像素数)`。

    为什么要这个而不是直接看截图：一个开关开着与关着长得一模一样，是那种
    「行为全对、只把状态弄丢了」的缺陷 —— 信号照发、值照存，`clipped_texts`
    那一类量尺寸的也管不着。本应用就中过一次（大厅「提示模式」与对局页两个
    开关都是可勾选的 QPushButton，而 QSS 里 `QPushButton` 没有 `:checked` 规则，
    实测两态 0 个像素不同）。

    第二个返回值是**自证测量可信**：同一个状态连抓两次应当一字不差。背景透明的
    控件 `grab()` 时可能拿到未初始化的残留像素，那种情况下上面的差值就是噪声，
    拿它当规格会 flaky —— 所以让调用方能看见这个数，而不是只相信第一个。

    量之前 `blockSignals`：这一条只问像素，不该顺带把棋盘重画一遍、
    更不该把用户的偏好写盘。最末一个 `finally` 把原状态摆回去。
    """
    was = box.isChecked()

    def shot():
        box.show()
        _process()
        return box.grab().toImage()

    box.blockSignals(True)
    try:
        box.setChecked(True)
        on = shot()
        on_again = shot()
        box.setChecked(False)
        off = shot()
    finally:
        box.blockSignals(False)
        box.setChecked(was)
    return _count_diff(on, off), _count_diff(on, on_again)


def chart_axis_ink(chart, side: str = "right", band: int = 30) -> int:
    """数某条纵轴的**标签带**里有多少个墨像素 —— 抓「标签被省略号化成 ···」。

    QtCharts 在标签带装不下时不报错也不警告，只把每个数字画成三个点（实测：
    目差轴 8 个刻度挤在 82px 高里 → 整列 9 个墨像素；同一张图 5~6 个刻度 → 58~84）。
    行为断言管不到它，只算几何（带宽 vs 字宽）也管不到 —— 带宽量出来是够的，
    画出来却还是被缩了。数墨是唯一诚实的口径：它不问画的是什么字，
    只问「这一列到底写没写出东西」。

    只扫绘图区那一段高度（轴标签就在那里），且从绘图区边缘往内退 2px：
    紧贴边缘的是轴线与刻度线，不退就会把它们算成标签的墨 —— 那时候不管
    标签画没画全都有墨，这个判据就只会绿（假阴性）。

    坐标一律乘 `devicePixelRatioF()`：`plotArea()` 是**逻辑**坐标而 `img` 是
    **设备**像素。不乘的后果不是「差几像素」，是整列量错地方：实测在
    DPR 1.5 的开发屏上，起点 `pa.right()+2` 落在绘图区**里面**，数到的墨
    是网格线与曲线（于是一条真被省略号化的轴也能拿 40+ 分），而标签带
    只划进去 20 逻辑像素 —— 同一个判据在两档 DPR 下一个假绿一个假红。
    """
    img = grab_image(chart)
    dpr = float(chart.devicePixelRatioF())
    pa = chart.chart().plotArea()
    bw = int(band * dpr)
    left = int(pa.right() * dpr) + 2 if side == "right" else max(
        0, int(pa.left() * dpr) - bw - 2)
    top, bottom = int(pa.top() * dpr), int(pa.bottom() * dpr)
    return sum(1 for y in range(top, bottom)
               for x in range(left, min(left + bw, img.width()))
               if img.pixelColor(x, y).lightness() < 170)


def clipped_texts(root: QWidget) -> tuple[list[str], int]:
    """返回 `(文字被裁掉的控件清单, 扫过的控件数)`。

    布局装不下时 Qt 不报错、不警告，只把控件压到 `sizeHint` 以下，文字两头各吃掉
    一截 —— 这类缺陷用行为断言永远测不到（按钮照样能点、信号照样发），
    只能看图。把看图机械化之后它才能不回归（首个发现的就是对局页那个
    「虚手（pass）」被画成「手（pass」）。

    只看**可见**的控件：隐藏的按钮还没被摆进位置，尺寸没有意义。
    开了 wordWrap 的 QLabel 排除：它本来就是靠换行来装长文字的，
    `sizeHint().width()` 对它是「不许换行时的宽度」，比实际宽一大截是正常。

    量之前先转几轮事件：数据回填（`setText`）与量尺寸常在同一个事件轮里，
    而布局重算靠的是 `setText` 投递的那个 LayoutRequest —— 不等就会量到
    上一次布局留下的旧宽度（实测：设置页的引擎徽章 40，再转一轮就是 136）。
    这不是给界面护短：转完之后还是装不下，那才是真裁字。
    """
    from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton

    offenders: list[str] = []
    scanned = 0
    _process()
    for w in root.findChildren(QWidget):
        # QCheckBox 也得扫：它是「一行文字 + 一个指示框」，比同文字的按钮还宽几像素，
        # 而挤不下时 Qt 同样不报错不警告，只把末尾截掉。
        if not isinstance(w, (QPushButton, QLabel, QCheckBox)) or not w.isVisible():
            continue
        text = w.text().strip()
        # getter 是 `wordWrap()`：C++ 那边的 `hasWordWrap()` 在 PySide6 里没绑出来
        # （写成 hasWordWrap 会直接 AttributeError，首跑就红在这里）。
        if not text or (isinstance(w, QLabel) and w.wordWrap()):
            continue
        scanned += 1
        need = w.sizeHint().width()
        parent = w.parent()
        # 父容器根本没有布局：这个控件漂在左上角默认 100x30 里，文字必然不够。最
        # 常见的成因是「给同一个控件装第二个布局」（Qt 只留一句 stderr 警告就丢掉）。
        # 这一条不看 wordWrap：开了换行的漂着控件同样画不对，不能从规格里漏掉。
        if isinstance(parent, QWidget) and parent.layout() is None:
            offenders.append(f"{type(w).__name__} {text!r}：父容器"
                             f" {type(parent).__name__} 没有布局（它漂在左上角）")
        elif w.width() + 1 < need:
            offenders.append(f"{type(w).__name__} {text!r}：宽 {w.width()} < 需要 {need}")
    return offenders, scanned


def bar_texts(root: QWidget) -> tuple[list[str], int]:
    """返回 `(槽内还在画字的进度条清单, 扫到的进度条总数)`。

    跟 `clipped_texts` 一样返回两个数：这一类控件默认是隐藏的（有任务才 show），
    只报违规清单的话「一个都没扫到」也会是绿，那个绿没有意义。

    QSS 关不掉进度条文字 —— 它没有 `text` 这个属性，写了只会收到一句
    `Unknown property text` 然后被丢掉（实测就是这句噪音刷了 29 遍）。
    只能在构造点 `setTextVisible(False)`，所以这里**扫全部**进度条：
    以后 P4/P5 新加的条子忘了关也会被抓到，而不是依赖人回去翻构造点。

    不只看可见的：进度条常常是「先建好、有任务了才 show」，没显出来不代表画错。
    """
    from PySide6.QtWidgets import QProgressBar

    bars = root.findChildren(QProgressBar)
    return ([f"{type(b).__name__}（值 {b.value()}）槽内还在画 {b.text()!r}"
             for b in bars if b.isTextVisible()], len(bars))
