"""设置页：引擎四态、大模型接入、教学偏好、音效、KataGo 安装入口。

对应 `frontend/src/pages/SettingsPage.tsx`（276 行）。数据只有两个 GET
（`/api/system/status` 与 `/api/auth/me`）加三个写接口（PATCH /api/auth/me、
PUT /api/auth/me/llm、POST /api/system/llm-test）。

引擎那一栏是本页最容易说错话的地方，四条口径逐条抄网页版并各留一条测试：
  · **`available` 优先于 `deathCount`**：看门狗把引擎拉起来之后 `deathCount` 仍留着
    历史计数，照网页版只看 `deathCount > 0` 就会在一台完全正常的机器上永远挂着
    「正在自动重启」—— 那是谎话，所以先问「现在能不能用」；
  · **`recover.watching` 不是「正在重启」**：它的实现是「看门狗任务还活着」，
    装了 KataGo 的机器上恒为 True。它只能用来区分「还在试」与「已经放弃」，
    不能用来判断引擎有没有问题（网页版把它放在 `deathCount > 0` 的分支里，
    恰好没出错，但那是侥幸）；
  · **预热不是故障**：`warming` 单独一态，否则刚起的服务看着像没装引擎；
  · **未安装不用错误色**：降级到启发式引擎后全流程仍可用（计划立的边界），
    红底会让学员以为程序坏了。这一处**主动偏离**网页版的红色文案。

另一处偏离：网页版的「安装方法」是一句要人手敲的命令行（`python katago/download.py`），
原生端给了真按钮 —— 桌面用户没有终端。见 `_start_install`。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import (QObject, QProcess, QProcessEnvironment, Qt, QTimer,
                            QUrl, Signal)
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QFrame, QGridLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QScrollArea, QSlider, QVBoxLayout, QWidget,
)

from core import paths
from core.sound import SOUND_NAMES
from ui import theme
from ui.widgets.parts import Alert, ElidedLabel, Panel, WrapLabel, hbox

#: 引擎五态。前四条就是计划里的「引擎四态」（就绪 / 预热中 / 断链自愈中 / 未装）；
#: 第五条 `stalled` 是「断链」那一支被网页版混在一起说的另一半 —— 看门狗已经放弃，
#: 此时界面必须改口（继续写「正在自动重启」会让学员一直等一个不会来的自愈）。
ENGINE_STATES = ("ready", "warming", "recovering", "stalled", "missing")

#: 态 → (徽章文字, 徽章色位)。色位取 `theme.BADGE_COLORS` 的键。
STATE_BADGE: dict[str, tuple[str, str]] = {
    "ready": ("KataGo 就绪", "ok"),
    "warming": ("KataGo 预热中", ""),             # 主色蓝：进行中，不是坏
    "recovering": ("断链自愈中", "warn"),
    "stalled": ("自动重启已停止", "err"),
    "missing": ("未安装 KataGo", "warn"),
}

#: 试听按钮。名字必须是 `core.sound.SOUND_NAMES` 里的（放错会被 SoundPlayer 直接 raise，
#: 那是有意的：拼错音效名是 bug，不该静默）。顺序与网页版一致。
PREVIEWS: tuple[tuple[str, str], ...] = (
    ("stone", "试听落子"), ("capture", "试听提子"), ("win", "试听胜利"),
    ("resign", "试听 AI 投子"), ("timeout", "试听超时"),
    ("promote", "试听晋升"),
)

LLM_HINT = (
    "任何 OpenAI 兼容接口都可以：DeepSeek、通义千问、Kimi、月之暗面，或本地 Ollama"
    "（接口地址填 http://localhost:11434/v1，API Key 任意非空值）。"
    "引擎负责算，大模型只负责把数据讲成人话；不配置也能出完整的数据报告。"
)

SOUND_HINT = (
    "音效是随包附带的 wav（落子、提子、胜负、AI 投子、超时、晋升、死活题对错各有其声），"
    "不联网、不牵扯版权。音量与开关存在本机（不是账号），换台电脑各调各的。"
)

INSTALL_HINT = (
    "点「安装 KataGo」会执行 backend/katago/download.py：联网下载引擎与网络权重"
    "（约 90MB+，取决于后端选择，可能要几分钟）。装好后自动切回 KataGo，不必重启。"
)

#: 安装跑起来之后顶掉上面那句。它说的「点下面的『安装 KataGo』」那时按钮位上已经是
#: 「停止」，再留着就是指错地方 —— 见 `_paint_engine` 里那段注释。
INSTALLING_HINT = (
    "正在安装：进度看下面的输出（约 90MB+，可能要几分钟）。"
    "跑完会自动重新读取引擎状态，不必重启，也不需要再点一次。"
)

LOG_TAIL = 400      # 安装输出只留最后这么多行，理由见 `installLog` 的 setMaximumBlockCount

#: 换行标签的宽度预算。`wordWrap` 的 QLabel 的 `sizeHint()` 是「不许换行时」的宽度，
#: 本页那几句说明都是 100+ 字（不封顶就是 1400px），量整个页面时会被它们顶爆：
#: 1280 窗口下内容视口只有 1112，不封顶整页就要横向滚动（而横向没得滚，
#: 只会静默裁字）。给个上限 = CSS 的 `max-width`，标签自己折行。
WRAP_MAX = 470


def wrap_label(text: str, parent: QWidget, role: str = "") -> QLabel:
    """一段会折行的说明文字。一律带上 `WRAP_MAX` 预算，理由见那里。

    用 `WrapLabel` 而不是裸 `QLabel`：后者在纵向吃紧时会被压到一行以下、把两行字画叠
    （见 parts.WrapLabel 的注释与用户报的那次）。
    """
    lab = WrapLabel(text, parent, max_width=WRAP_MAX)
    if role:
        lab.setProperty("role", role)
    return lab


# ---------------------------------------------------------------- 纯函数（口径都在这里）

def engine_state(engine: dict | None) -> str:
    """把 `/api/system/status` 的 engine 段折成 `ENGINE_STATES` 里的一个。

    单独成函数而不是埋在绘制里：这四个分支是本页唯一会**说错话**的地方，
    必须能脱离界面逐个喂数据验（旧后端没有 `recover`/`deathCount` 字段时
    也要能给出答复，那种情况下取不到就是 0 / False，落到 `missing` 或 `ready`）。
    """
    eng = engine or {}
    kat = eng.get("katago") or {}
    if kat.get("available"):
        return "ready"
    if eng.get("warming"):
        return "warming"
    if int(kat.get("deathCount") or 0) > 0:
        return "recovering" if (eng.get("recover") or {}).get("watching") else "stalled"
    return "missing"


def engine_detail(engine: dict | None) -> tuple[str, str]:
    """(状态正文, 补充说明)。补充说明为空串时那一行不显示。"""
    eng = engine or {}
    kat = eng.get("katago") or {}
    rec = eng.get("recover") or {}
    state = engine_state(eng)
    deaths = int(kat.get("deathCount") or 0)
    mx = int(rec.get("maxAttempts") or 5)
    if state == "ready":
        # 已自愈过就说一句：这既是事实，也让「刚才那次卡顿」有据可查
        tail = f"本轮已自愈 {deaths} 次，无需重装。" if deaths else ""
        return ("引擎已就绪，段位档位与高精度复盘都走 KataGo。", tail)
    if state == "warming":
        return ("KataGo 预热中（首次运行要做 GPU 内核调优，可能要几分钟），就绪后自动启用。",
                "预热期间查询走内置启发式引擎，不影响对局。")
    reason = kat.get("error") or kat.get("lastDeathReason") or "原因未知"
    when = kat.get("lastDeathAt") or ""
    head = (f"引擎已安装，但运行中断链 {deaths} 次"
            + (f"（最近一次 {when}）" if when else "")
            + f"：{reason}")
    if state == "recovering":
        attempt = int(rec.get("attempt") or 1)
        return (head, f"正在自动重启（第 {attempt}/{mx} 次），成功后会自动切回 KataGo，无需重装。")
    if state == "stalled":
        # 早先这里指了一条 stderr 日志的绝对路径 —— 部署细节；学员该知道的
        # 只是「没在继续试了 + 下一步做什么」（第 33 轮清除开发者痕迹）。
        return (head, "自动重启已停止，不必重装。可尝试重启应用；若仍然失败，"
                      "把「关于」里的日志位置发给支持排查。")
    err = (kat.get("error") or "").strip()
    return (err or "未检测到可用的 KataGo，当前使用内置启发式引擎。",
            "安装方法：点下面的「安装 KataGo」。装好后级位～九段全部档位与"
            "高精度复盘都会自动启用。")


def _num(value) -> str:
    """把接口给的数字洗成一行可读中文。

    为何不直写 `{value}`：后端这些字段是 float，Python 会打 30.0 / 6.0，
    而网页版（JS）打 30 / 6 —— 两个客户端同一个数字长两个样就会被当成两个口径。
    `:g` 顺带去掉无意义的零。拿不到数字就退回原文（接口改了形状不在这里编造）。"""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return "—" if value is None else str(value)


def resign_text(resign: dict | None) -> str:
    r = resign or {}
    wr = float(r.get("winrateThreshold") or 0)
    return (f"连续 {r.get('consecutiveMoves', '—')} 手胜率 <{int(round(wr * 100))}%"
            f" 且落后 {_num(r.get('scoreThreshold'))} 目时投子")


def review_text(review: dict | None) -> str:
    rv = review or {}
    th = rv.get("thresholds") or {}
    return (f"每手 {rv.get('visits', '—')} 次推演；缓手 ≥ {_num(th.get('slow'))} 目、"
            f"恶手 ≥ {_num(th.get('bad'))} 目、大恶手 ≥ {_num(th.get('blunder'))} 目")


def demotion_text(demotion: dict | None) -> str:
    return f"开启降级（连败 {int((demotion or {}).get('losingStreak') or 4)} 场降 1 级）"


def missing_sounds(sounds_dir) -> tuple[str, ...]:
    """资源目录里缺哪几个音效。

    不去读 `SoundPlayer.missing`：那是**播放过才知道**的懒账，设置页一进来就该
    把「这套资源不完整」说清楚，而不是等用户点了试听才发现某一声从来就没响过。
    """
    d = sounds_dir if isinstance(sounds_dir, Path) else Path(str(sounds_dir))
    return tuple(n for n in SOUND_NAMES if not (d / f"{n}.wav").exists())


def volume_percent(value: float) -> int:
    """滑杆读数 → 百分比。0.075 这种浮点尾数不能漏进文案。"""
    return int(round(max(0.0, min(1.0, float(value))) * 100))


# ---------------------------------------------------------------- 安装 KataGo

class InstallRunner(QObject):
    """`python katago/download.py` 的包装。

    用 QProcess 而不是 subprocess + 线程：Qt 自己会把子进程的输出与退出排回主线程，
    不必在跨线程信号上再发明一层。只暴露 `start()/stop()` 与两个信号，
    测试因此可以塞一个假的进来（见 SettingsPage.installer）。
    """

    output = Signal(str)
    finished = Signal(int, str)          # (退出码, QProcess 给的错误描述)

    #: 用户按「停止」后给子进程的宽限期（毫秒）：到点还没退就 kill。
    STOP_GRACE_MS = 1200

    def __init__(self, parent=None, program: str = "", args=None, workdir=None):
        super().__init__(parent)
        self._stop_requested = False     #: 这次收尾是「用户主动停的」，不是失败
        self._kill_timer = None
        self._p = QProcess(self)
        self._p.setProgram(program or sys.executable)
        self._p.setArguments(list(args or ["katago/download.py"]))
        self._p.setWorkingDirectory(str(workdir or paths.BACKEND_DIR))
        self._p.setProcessChannelMode(QProcess.MergedChannels)
        # 子进程的输出被管道接管（不是终端），Windows 下 Python 会按 ANSI 代码页写；
        # 强制 UTF-8 才能和上面的 decode("utf-8") 对上，否则中文进度全是问号。
        # 环境变量必须从 `QProcessEnvironment.systemEnvironment()` 拿，不是
        # `QProcess.systemEnvironment()`：后者在 PySide6 6.11 里返回的是一个**普通 list**
        # （`NAME=value` 字符串列表），对它调 `insert("A", "1")` 命中的是
        # `list.insert(index, obj)`，症状是一句 `TypeError: 'str' object cannot be
        # interpreted as an integer`，错在构造安装器那一步，看着像界面 bug。
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUTF8", "1")
        env.insert("PYTHONUNBUFFERED", "1")   # 不缓冲，进度才是一点点冒出来而不是最后糊一屏
        self._p.setProcessEnvironment(env)
        self._p.readyReadStandardOutput.connect(self._on_output)
        self._p.finished.connect(self._on_end)

    @property
    def proc(self) -> QProcess:
        """子进程本体。验收要验「到底会跑什么命令、在哪个目录、带哪几个环境变量」，
        这些都在 `QProcess` 上 —— 与其让测试去摸 `_p`，不如留一个说得出用途的口子。"""
        return self._p

    @property
    def stop_requested(self) -> bool:
        """用户按过「停止」：收尾时按「已停止」报，而不是「安装失败」（审计 M6）。"""
        return self._stop_requested

    def start(self) -> None:
        self._stop_requested = False
        self._p.start()

    def stop(self) -> None:
        """请求停止。**不阻塞界面**（审计 M6）。

        从前是 `terminate()` + `waitForFinished(3000)`：在 GUI 线程里干等 3 秒，而
        Windows 下 `terminate()` 对控制台子进程基本无效 —— 那 3 秒必然白等，
        等满之后才 kill，于是「停止」按钮每次都要冻住界面 3 秒。现在只发
        `terminate()` 并挂一个宽限定时器：到点还没退再 kill，全程不阻塞。
        """
        if self._p.state() == QProcess.NotRunning:
            return
        self._stop_requested = True
        self._p.terminate()
        if self._kill_timer is None:
            self._kill_timer = QTimer(self)
            self._kill_timer.setSingleShot(True)
            self._kill_timer.timeout.connect(self._kill_now)
        self._kill_timer.start(self.STOP_GRACE_MS)

    def _kill_now(self) -> None:
        if self._p.state() != QProcess.NotRunning:
            self._p.kill()

    @property
    def running(self) -> bool:
        return self._p.state() != QProcess.NotRunning

    def _on_output(self) -> None:
        raw = bytes(self._p.readAllStandardOutput())
        if raw:
            self.output.emit(raw.decode("utf-8", "replace"))

    def _on_end(self, code: int, _status) -> None:
        self.finished.emit(int(code), self._p.errorString())


# ---------------------------------------------------------------- 页面

class SettingsPage(QWidget):
    """设置页。"""

    def __init__(self, api, sound, prefs, parent=None, chrome=None):
        super().__init__(parent)
        self._api = api
        self._sound = sound
        self._prefs = prefs
        #: 应用级动作的持有者（菜单栏撤掉后由本页提供可见入口）。可以不传：
        #: 那样「应用」卡片里的按钮会退化成一句错误提示，其余部分照常工作。
        self._chrome = chrome
        self.appButtons: dict[str, QPushButton] = {}
        self._status: dict = {}
        self._user: dict = {}
        self.installer = None                      #: 安装中的 InstallRunner；没在装就是 None
        self._build_ui()

    # ---------------------------------------------------------------- 装配

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        head = hbox(10)
        title = QLabel("设置", self)
        title.setProperty("role", "title")
        #: 当前登录名。半透明：它是「谁在用」的归属信息，不该和可点的按钮抢注意力
        #: （窗口标题里已经不再带用户名 —— 标题只报应用名）。
        # 用带 alpha 的字色而不是 `QGraphicsOpacityEffect`：效果器会让父控件走离屏渲染，
        # 实测会留下残影（用户报的「字符重叠」有一半嫌疑在它身上）；字色 alpha 没有这个副作用。
        self.lblUser = QLabel("", self)
        self.lblUser.setProperty("role", "muted")
        self.lblUser.setStyleSheet(f"color: {theme.USER_TAG};")
        self.btnReload = QPushButton("重新读取", self)
        self.btnReload.setCursor(Qt.PointingHandCursor)
        self.btnReload.clicked.connect(self.refresh)
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self.lblUser)
        head.addWidget(self.btnReload)
        root.addLayout(head)

        self.errorBar = Alert("err", "", self)
        self.msg = Alert("ok", "", self)
        # 三路来源各自的错误槽（审计 M7）：错误条从前**只增不减** —— 重试成功了
        # 它也不消失，界面就长期挂着一条与内容矛盾的失败提示。分槽是因为本页
        # 两个 GET 互相独立，其中一路成功不该抹掉另一路的错误。
        self._err_status = ""
        self._err_me = ""
        self._err_other = ""
        root.addWidget(self.errorBar)
        root.addWidget(self.msg)

        body = hbox(14)
        # 内容收进纵向滚动区（第 33 轮）：本页在 1280x800 下天然比视口高过
        # 一百多像素 —— 早先靠面板内部的网格「吸收」缺口，缺口落在谁头上谁
        # 被压扁（折行标签会被压到一行以下、把第二行画叠在第一行上）。
        # 放进滚动区后内容按真实高度展开，视口不够就出滚动条，而不是无声地
        # 压扁某个控件（对齐对局/死活/复盘侧栏的既有做法）。
        body.addWidget(self._build_left(), 47)
        body.addWidget(self._build_right(), 53)
        self.scrollArea = QScrollArea(self)
        # widgetResizable=False：内容是「自然高度」而不是「视口高度」——
        # True 会让内容被拉/压到视口大小，纵向不足时又开始压扁折行标签
        # （第 33 轮实测，见 test_no_wrapped_label_is_squeezed_below_its_text）。
        self.scrollArea.setWidgetResizable(False)
        self.scrollArea.setFrameShape(QFrame.NoFrame)
        self.scrollArea.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setLayout(body)
        self.scrollArea.setWidget(holder)
        root.addWidget(self.scrollArea, 1)

    def _build_left(self) -> QWidget:
        col = QWidget(self)
        v = QVBoxLayout(col)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)
        v.addWidget(self._build_llm_panel())
        v.addWidget(self._build_sound_panel())
        v.addStretch(1)
        return col

    def _build_right(self) -> QWidget:
        col = QWidget(self)
        v = QVBoxLayout(col)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)
        v.addWidget(self._build_engine_panel())
        v.addWidget(self._build_prefs_panel())
        v.addWidget(self._build_app_panel())
        v.addStretch(1)
        return col

    def _build_app_panel(self) -> QWidget:
        """应用级动作（菜单栏撤掉之后的落点）。

        按钮**不自己实现任何东西**：它们 `trigger()` 的是 `chrome.actions` 里那几个
        动作本体。菜单栏在的时候由它持有这些动作，现在换成本页提供可见入口 ——
        实现仍然只有一份，快捷键也照旧挂在窗口上。
        """
        p = Panel("应用", self)
        rows = (
            ("keys", "键盘快捷键"),
            ("about", "关于"),
            ("quit", "退出应用"),
        )
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        for i, (key, text) in enumerate(rows):
            btn = QPushButton(text, p)
            btn.setCursor(Qt.PointingHandCursor)
            if key == "quit":
                btn.setProperty("role", "danger")
            btn.clicked.connect(lambda _=False, k=key: self._trigger_app(k))
            # 一行摆完：多加一行会把设置页在 1280x800 下顶出纵向滚动条
            # （`test_no_page_needs_a_scrollbar` 当场报红，实测 793 > 769）。
            grid.addWidget(btn, 0, i)
            self.appButtons[key] = btn
        p.body.addLayout(grid)
        p.body.addWidget(wrap_label(
            "快捷键照旧有效（F1 查全部）。", p, "muted"))
        return p

    def _set_page_error(self, source: str, text: str) -> None:
        """更新某一路错误并重画错误条（审计 M7）。`text` 传空串 = "这一路好了"。

        只增不减的反例就在本页：`refresh()` 失败一次，之后每次刷新成功，
        红条还挂在那里说"读取失败"，用户以为这页坏了。
        """
        setattr(self, f"_err_{source}", text or "")
        self.errorBar.show_text(
            self._err_status or self._err_me or self._err_other, "err")

    def _trigger_app(self, key: str) -> None:
        """按动作键触发窗口级动作（不另写实现）。"""
        chrome = self._chrome
        if chrome is None or key not in chrome.actions:
            self._set_page_error("other", "应用动作没接上（内部错误：chrome 未注入）")
            return
        chrome.actions[key].trigger()

    def _field(self, parent: QWidget, label: str, edit: QLineEdit) -> None:
        """一行「标题 + 输入框」。网页版的 `.field` 是竖排，这里同构。"""
        cap = QLabel(label, parent)
        cap.setProperty("role", "h3")
        edit.setClearButtonEnabled(True)
        parent_lay = parent.layout()
        parent_lay.addWidget(cap)
        parent_lay.addWidget(edit)

    def _build_llm_panel(self) -> QWidget:
        p = Panel("大模型接入（复盘讲解）", self)
        p.body.setSpacing(6)
        p.body.addWidget(wrap_label(LLM_HINT, p, "muted"))

        self.edBaseUrl = QLineEdit(p)
        self.edBaseUrl.setPlaceholderText("https://api.deepseek.com/v1")
        self.edModel = QLineEdit(p)
        self.edModel.setPlaceholderText("deepseek-chat / qwen-plus / kimi-k2 …")
        self.edApiKey = QLineEdit(p)
        self.edApiKey.setEchoMode(QLineEdit.Password)
        self.edApiKey.setPlaceholderText("sk-…")
        self._field(p, "接口地址 baseUrl", self.edBaseUrl)
        self._field(p, "模型名", self.edModel)

        cap = hbox(8)
        lab = QLabel("API Key", p)
        lab.setProperty("role", "h3")
        self.keyBadge = QLabel("已配置", p)
        self.keyBadge.setProperty("role", "badge")
        self.keyBadge.setStyleSheet(theme.badge_style("ok"))
        self.keyBadge.setVisible(False)
        cap.addWidget(lab)
        cap.addWidget(self.keyBadge)
        cap.addStretch(1)
        p.body.addLayout(cap)
        p.body.addWidget(self.edApiKey)

        row = hbox(8)
        self.btnSaveLlm = QPushButton("保存配置", p)
        self.btnSaveLlm.setProperty("role", "primary")
        self.btnTestLlm = QPushButton("测试连接", p)
        self.btnClearKey = QPushButton("清除 Key", p)
        self.btnClearKey.setProperty("role", "danger")
        self.btnClearKey.setVisible(False)
        for b, slot in ((self.btnSaveLlm, self._save_llm),
                        (self.btnTestLlm, self._test_llm),
                        (self.btnClearKey, self._clear_key)):
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(slot)
        row.addWidget(self.btnSaveLlm)
        row.addWidget(self.btnTestLlm)
        row.addWidget(self.btnClearKey)
        row.addStretch(1)
        p.body.addLayout(row)
        return p

    def _build_sound_panel(self) -> QWidget:
        p = Panel("音效", self)
        p.body.setSpacing(6)
        p.body.addWidget(wrap_label(SOUND_HINT, p, "muted"))

        self.chkSound = QCheckBox("开启音效", p)
        self.chkSound.toggled.connect(self._on_sound_toggled)
        p.body.addWidget(self.chkSound)
        self.lackBar = Alert("warn", "", p)
        p.body.addWidget(self.lackBar)

        row = hbox(10)
        cap = QLabel("音量", p)
        cap.setProperty("role", "h3")
        self.sldVolume = QSlider(Qt.Horizontal, p)
        self.sldVolume.setRange(0, 100)
        self.sldVolume.setSingleStep(5)
        self.sldVolume.setPageStep(10)
        self.sldVolume.valueChanged.connect(self._on_volume_changed)
        self.sldVolume.sliderReleased.connect(self._preview_volume)
        self.volLabel = QLabel("70%", p)
        self.volLabel.setProperty("role", "muted")
        self.volLabel.setMinimumWidth(40)
        row.addWidget(cap)
        row.addWidget(self.sldVolume, 1)
        row.addWidget(self.volLabel)
        p.body.addLayout(row)

        grid = QGridLayout()
        grid.setContentsMargins(0, 4, 0, 0)
        grid.setHorizontalSpacing(8)
        self.btnPreview: dict[str, QPushButton] = {}
        for i, (name, text) in enumerate(PREVIEWS):
            b = QPushButton(text, p)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, n=name: self._preview(n))
            self.btnPreview[name] = b
            grid.addWidget(b, i // 3, i % 3)
        p.body.addLayout(grid)
        return p

    def _build_prefs_panel(self) -> QWidget:
        p = Panel("教学偏好", self)
        self.chkHint = QCheckBox("对局中显示引擎推荐点（关闭可锻炼独立计算）", p)
        self.chkHint.toggled.connect(lambda _=False: self._patch_profile("hintMode",
                                                                        self.chkHint.isChecked()))
        self.chkDemotion = QCheckBox("开启降级", p)
        self.chkDemotion.toggled.connect(lambda _=False: self._patch_profile(
            "demotionEnabled", self.chkDemotion.isChecked()))
        p.body.addWidget(self.chkHint)
        p.body.addWidget(self.chkDemotion)
        return p

    def _build_engine_panel(self) -> QWidget:
        p = Panel("引擎与对局规则", self)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        self.kv: dict[str, QWidget] = {}
        # 只留「当前引擎」一行：引擎路径 / 权重路径 / human SL 是给部署者看的
        # 技术细节（绝对路径 + 文件后缀），学员读不懂也做不了任何事 ——
        # 第 33 轮清除开发者痕迹时删掉的，别再加回来。
        val = ElidedLabel("—", p)
        val.setMaximumWidth(360)
        grid.addWidget(QLabel("当前引擎", p), 0, 0, Qt.AlignTop)
        grid.addWidget(val, 0, 1)
        self.kv["当前引擎"] = val
        p.body.addLayout(grid)

        badge_row = hbox(8)
        self.engineBadge = QLabel("—", p)
        self.engineBadge.setProperty("role", "badge")
        self.engineBadge.setStyleSheet(theme.badge_style("muted"))
        badge_row.addWidget(self.engineBadge)
        badge_row.addStretch(1)
        p.body.addLayout(badge_row)

        self.engineDetail = wrap_label("—", p)
        p.body.addWidget(self.engineDetail)
        self.engineHint = wrap_label("", p, "muted")
        self.engineHint.setVisible(False)
        p.body.addWidget(self.engineHint)

        self.rulesLine = wrap_label("—", p, "muted")
        p.body.addWidget(self.rulesLine)

        self.accuracyLine = wrap_label("—", p, "muted")
        p.body.addWidget(self.accuracyLine)

        # ---------------- 安装入口。网页版这里只有一句要人手敲的命令，桌面用户没有终端。
        self.installTip = wrap_label(INSTALL_HINT, p, "muted")
        p.body.addWidget(self.installTip)

        row = hbox(8)
        self.btnInstall = QPushButton("安装 KataGo", p)
        self.btnInstall.setProperty("role", "primary")
        self.btnInstall.setCursor(Qt.PointingHandCursor)
        self.btnInstall.clicked.connect(self._start_install)
        self.btnStop = QPushButton("停止", p)
        self.btnStop.setCursor(Qt.PointingHandCursor)
        self.btnStop.clicked.connect(self._stop_install)
        self.btnStop.setVisible(False)
        self.btnLog = QPushButton("打开日志目录", p)
        self.btnLog.setCursor(Qt.PointingHandCursor)
        self.btnLog.clicked.connect(self._open_logs)
        row.addWidget(self.btnInstall)
        row.addWidget(self.btnStop)
        row.addWidget(self.btnLog)
        row.addStretch(1)
        p.body.addLayout(row)

        #: 安装输出。`setMaximumBlockCount` 是必需的：download.py 在非终端下每次刷新
        #: 都占一行（脚本里那段 `IS_TTY` 注释说的就是这件事），几百行进度会把真正的
        #: 错误信息洗掉 —— 而这正是需要读它的唯一时机。
        self.installLog = QPlainTextEdit(p)
        self.installLog.setReadOnly(True)
        self.installLog.setMaximumBlockCount(LOG_TAIL)
        self.installLog.setFixedHeight(96)
        self.installLog.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")
        self.installLog.setVisible(False)
        p.body.addWidget(self.installLog)
        return p

    # ---------------------------------------------------------------- 数据

    def refresh(self) -> None:
        """两个 GET 各管一半：引擎/规则来自 status，配置与偏好来自 me。

        不分开发的话，任一个失败就会把整页挡住 —— 而这两半互相独立，
        没理由让「读不到 LLM 配置」连带引擎状态也看不见。
        """
        self._api.get("/api/system/status").finished.connect(self._on_status)
        self._api.get("/api/auth/me").finished.connect(self._on_me)

    def _on_status(self, data, err) -> None:
        if err is not None or not isinstance(data, dict):
            self._set_page_error("status", f"读取引擎与规则参数失败：{err}")
            return
        self._set_page_error("status", "")      # 这一路好了就收红条（审计 M7）
        self._status = data
        self._paint_engine()

    def _on_me(self, data, err) -> None:
        user = (data or {}).get("user") if isinstance(data, dict) else None
        if err is not None or not user:
            self._set_page_error("me", f"读取账号配置失败：{err}")
            return
        self._set_page_error("me", "")          # 同上（审计 M7）
        self._user = user
        self._paint_user()
        self._paint_llm()
        self._paint_prefs()

    def _paint_user(self) -> None:
        name = (self._user.get("displayName") or self._user.get("username") or "").strip()
        self.lblUser.setText(f"当前登录：{name}" if name else "")

    # ---------------------------------------------------------------- 绘制

    def _paint_engine(self) -> None:
        eng = self._status.get("engine") or {}
        kat = eng.get("katago") or {}
        state = engine_state(eng)
        # 「正在装」是**客户端**的事实（接口那边只会一直报 missing），所以它得参与
        # 整栏的绘制：按钮位与两句指着「安装 KataGo」按钮的文案都要跟着改口。
        installing = self.installer is not None
        text, kind = STATE_BADGE[state]
        self.engineBadge.setText(text)
        self.engineBadge.setStyleSheet(theme.badge_style(kind))
        head, tail = engine_detail(eng)
        if installing:
            # 未装那一态的补充说明是「安装方法：点下面的『安装 KataGo』」—— 装到一半
            # 按钮位上已经是「停止」，这句就成了指错地方的话；进度与「不必再点一次」
            # 由下面的 `installTip` 说，这里不重复一遍。
            tail = ""
        self.engineDetail.setText(head)
        self.engineHint.setText(tail)
        self.engineHint.setVisible(bool(tail))

        active = eng.get("active") or ""
        self.kv["当前引擎"].setText("KataGo" if active == "katago" else "内置启发式引擎")

        self.rulesLine.setText("AI 认输：" + resign_text(self._status.get("resign")))
        self.accuracyLine.setText("复盘精度：" + review_text(self._status.get("review")))
        self.chkDemotion.setText(demotion_text(self._status.get("demotion")))

        # 装好了就没必要再给一个「再下一次 90MB」的按钮；预热/自愈中同理。
        # 但安装进行中两个按钮都不能同时上位：`refresh()` 会在切回本页时再跑一次
        # 这里，而那时 `state` 还是 `missing`（引擎还没装完），不挡住就会
        # 「安装 KataGo」与「停止」并排出现 —— 再点一次会被 `_start_install` 拦住，
        # 但界面已经在说谎。绘制必须是状态的纯函数，不能只靠 `_start_install`
        # 里那几下 setVisible。
        self.btnStop.setVisible(installing)
        self.btnInstall.setVisible(state in ("missing", "stalled") and not installing)
        self.installTip.setText(INSTALLING_HINT if installing else INSTALL_HINT)
        self.installTip.setVisible(installing or self.btnInstall.isVisible())

    def _paint_llm(self) -> None:
        cfg = (self._user.get("llmConfig") or {})
        has_key = bool(cfg.get("hasApiKey"))
        # 只回填**没被人改过**的那几栏（`isModified`）：安装跑完会顺手 refresh 一次，
        # 无条件 setText 会把手正在打的 Key 冲掉，还会把滑杆那种「本地值与服务端不同」
        # 的覆盖也抹了。用 isModified 而不是 hasFocus：点「保存」时焦点已经离开了输入框，
        # 而那一栏恰恰是最容易被刷新冲掉的（offscreen 平台下 hasFocus 恒为 False，
        # 拿焦点当判据连测试都没法写）。保存成功后清账（见 `_on_llm_saved`）。
        if not self.edBaseUrl.isModified():
            self.edBaseUrl.setText(cfg.get("baseUrl") or "")
        if not self.edModel.isModified():
            self.edModel.setText(cfg.get("model") or "")
        if not self.edApiKey.isModified():
            self.edApiKey.clear()            # 后端不回传明文，永远留空
        self.edApiKey.setPlaceholderText("留空表示不修改" if has_key else "sk-…")
        self.keyBadge.setVisible(has_key)
        self.btnClearKey.setVisible(has_key)

    def _paint_prefs(self) -> None:
        """把账号上的两个开关摆进界面。

        `blockSignals` 是必需的：不拦的话这次回填会立刻触发一次 PATCH，
        界面自己把自己写了回去（而 PATCH 的回复又会再来一次回填 —— 一个环）。"""
        defaults = {"hintMode": True, "demotionEnabled": False}
        # 默认值分叉是抄网页版的 `?? true` / `?? false`：提示点默认开，
        # 降级默认关（教学模式的既定口径）。字段缺位时不能两个都当 False。
        for box, key in ((self.chkHint, "hintMode"), (self.chkDemotion, "demotionEnabled")):
            box.blockSignals(True)
            box.setChecked(bool(self._user.get(key, defaults[key])))
            box.blockSignals(False)

    def _paint_sound(self) -> None:
        # 回填一律 `blockSignals`：`showEvent` 每次回到本页都会跑这里，不拦的话
        # 「看一眼设置页」就等于「把音效开关又写了一遍盘 + 响一声」，
        # 音量滑杆同理（会把刚读上来的本机值再写回去一次）。与 `_paint_prefs` 同一个坑。
        self.chkSound.blockSignals(True)
        self.chkSound.setChecked(self._prefs.sound_enabled)
        self.chkSound.blockSignals(False)
        pct = volume_percent(self._prefs.volume)
        self.sldVolume.blockSignals(True)
        self.sldVolume.setValue(pct)
        self.sldVolume.blockSignals(False)
        self.volLabel.setText(f"{pct}%")
        for b in self.btnPreview.values():
            b.setEnabled(self._prefs.sound_enabled)
        lack = missing_sounds(self._sound.sounds_dir)
        self.lackBar.show_text(
            f"缺 {len(lack)} 个音效文件（{'、'.join(lack)}），这些时刻会静默。" if lack else "")

    # ---------------------------------------------------------------- 写：大模型

    def _save_llm(self) -> None:
        body = {"baseUrl": self.edBaseUrl.text().strip(),
                "model": self.edModel.text().strip()}
        key = self.edApiKey.text().strip()
        if key:
            body["apiKey"] = key        # 不传就是不改：与网页版同一条口径
        self._busy(True)
        self._api.put("/api/auth/me/llm", body).finished.connect(self._on_llm_saved)

    def _on_llm_saved(self, data, err) -> None:
        self._busy(False)
        if err is not None:
            self.msg.show_text(f"保存失败：{err}", "err")
            return
        cfg = (data or {}).get("llmConfig") or {}
        if cfg:
            self._user["llmConfig"] = cfg
            # 保存成功 = 本地草稿已经交上去了，不再是「未保存的修改」：三栏一并清账，
            # `_paint_llm` 才敢回填。Key 那一栏必须空：接口不回传明文，留着的只有
            # 泄漏面（截图、远程协助、录屏）而没有别的用处。
            for edit in (self.edBaseUrl, self.edModel, self.edApiKey):
                edit.setModified(False)
            self.edApiKey.clear()
            self._paint_llm()
        self.msg.show_text("大模型配置已保存", "ok")

    def _test_llm(self) -> None:
        """连通测试用的是**服务器上那份**配置，不是输入框里没保存的草稿。

        这不是偷懒：`/api/system/llm-test` 没有请求体，它读的是已落盘的
        `user.llm_config`。所以「改了没保存就点测试」测的是旧值 —— 界面必须说清楚，
        否则学员会以为新 Key 通了（或反过来）。文案见 `_on_test_reply`。
        """
        self._busy(True)
        self.msg.show_text("")
        self._api.post("/api/system/llm-test").finished.connect(self._on_test_reply)

    def _on_test_reply(self, data, err) -> None:
        self._busy(False)
        if err is not None:
            self.msg.show_text(f"连接失败：{err}", "err")
            return
        ok = bool((data or {}).get("ok"))
        self.msg.show_text((data or {}).get("message") or
                           ("连接成功" if ok else "连接失败"), "ok" if ok else "err")

    def _clear_key(self) -> None:
        self._busy(True)
        self._api.put("/api/auth/me/llm", {"apiKey": ""}).finished.connect(self._on_key_cleared)

    def _on_key_cleared(self, data, err) -> None:
        self._busy(False)
        if err is not None:
            self.msg.show_text(f"清除失败：{err}", "err")
            return
        self._user["llmConfig"] = (data or {}).get("llmConfig") or {}
        self._paint_llm()
        self.msg.show_text("已清除 API Key（复盘将改用模板讲解）", "ok")

    def _busy(self, busy: bool) -> None:
        for b in (self.btnSaveLlm, self.btnTestLlm, self.btnClearKey):
            b.setEnabled(not busy)

    # ---------------------------------------------------------------- 写：偏好与音效

    def _patch_profile(self, field: str, value) -> None:
        if not self._user:
            return                        # 还没读到账号就摆不动开关，也就谈不上写回去
        self._api.patch("/api/auth/me", {field: value}).finished.connect(self._on_profile_patched)

    def _on_profile_patched(self, data, err) -> None:
        if err is not None:
            self.msg.show_text(f"偏好没保存成功：{err}", "err")
            return
        user = (data or {}).get("user") or {}
        if user:
            self._user = user
            # 以回包为准重摆一次开关：用户点的那一下只是**请求**，服务端说了才算。
            # 不回填的话，“服务端拒了这次修改”会表现为开关停在一个服务端不认的
            # 位置上，而且一直说到下次重进本页 —— 中间每次看一眼都是错的。
            # `_paint_prefs` 自己拦了信号，不会绕回这里。
            self._paint_prefs()
        self.msg.show_text("已保存", "ok")

    def _on_sound_toggled(self, on: bool) -> None:
        self._prefs.sound_enabled = on
        self._prefs.sync()
        for b in self.btnPreview.values():
            b.setEnabled(on)
        if on:
            self._preview("click")           # 一打开就先给一声，确认这台设备真有声音

    def _on_volume_changed(self, pct: int) -> None:
        self._prefs.volume = pct / 100.0
        self.volLabel.setText(f"{pct}%")

    def _preview_volume(self) -> None:
        """松手才出声：拖动过程中每 1% 都放一次会很吵。"""
        self._prefs.sync()
        self._preview("click")

    def _preview(self, name: str) -> None:
        self._sound.play(name)

    # ---------------------------------------------------------------- 安装 KataGo

    def _start_install(self) -> None:
        if self.installer is not None:
            return
        runner = self._make_installer()
        self.installer = runner
        runner.output.connect(self._on_install_output)
        runner.finished.connect(self._on_install_finished)
        self.installLog.clear()
        self.installLog.setVisible(True)
        self._append_log("[安装已开始]")
        self.btnReload.setEnabled(False)
        self._paint_engine()          # 按钮位与提示文案都归它算，不在这里手动摸 setVisible
        runner.start()

    def _make_installer(self):
        """装一个安装器。测试把它换成假的（口径见类文档字符串）。"""
        return InstallRunner(parent=self)

    def _on_install_output(self, text: str) -> None:
        self._append_log(text)

    def _append_log(self, text: str) -> None:
        self.installLog.appendPlainText(text.rstrip("\n"))

    def _on_install_finished(self, code: int, message: str) -> None:
        # 先读「是不是用户主动停的」再清引用（审计 M6）：主动停下来的退出码
        # 一定非 0，照旧报「安装没有完成」就是在把用户的意图说成故障。
        stopped = bool(getattr(self.installer, "stop_requested", False))
        self.installer = None
        self.btnReload.setEnabled(True)
        self._paint_engine()          # 按钮位交还给「安装 KataGo」（理由见上面那条注释）
        if stopped:
            self._append_log("[已停止]")
            self.msg.show_text("已停止安装。引擎还没装上，可以随时重新开始。", "warn")
            self.refresh()
            return
        if code == 0:
            self._append_log("[安装完成]")
            self.msg.show_text("KataGo 安装脚本已跑完，正在重新读取引擎状态…", "ok")
            self.refresh()
        else:
            tail = message or ""
            # 退出码是给开发者看的：玩家只需要知道「没装上 + 去哪看原因」。
            self._append_log(f"[安装失败]{('：' + tail) if tail else ''}")
            self.msg.show_text("安装没有完成，原因见上方输出；可以稍后重试。", "err")

    def _stop_install(self) -> None:
        if self.installer is not None:
            self._append_log("[正在停止安装…]")
            self.installer.stop()

    def _open_logs(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.LOGS_DIR)))

    # ---------------------------------------------------------------- 收尾

    def showEvent(self, ev):                                  # noqa: N802
        """第一次显示时把音效那半摆出来。

        音效状态不在 `refresh()` 里画：它是本机的、不是服务器的，
        而 `Prefs` 与资源目录在构造时就已经可用 —— 但那时控件还没进可视树，
        量出来的 `sounds_dir` 与开关状态没意义，故留到这里。
        """
        super().showEvent(ev)
        self._paint_sound()

    def shutdown(self) -> None:
        """关窗前收掉子进程：不然 download.py 会成为一个孤儿，继续占着网络。"""
        if self.installer is not None:
            self.installer.stop()
            self.installer = None
