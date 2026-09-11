"""设置页的验收：引擎四态各自说对了话、写接口发的包逐字对上、安装那条链能收尾。

为什么不打后端：这一页真正会伤人的是**状态措辞**与**写回去的包**：
  · 「正在自动重启」挂在一台已经自愈的机器上 = 让学员一直等；
  · 「保存」把一个空的 apiKey 发上去 = 把人家配好的 Key 清了（后端把空串当清除）；
  · 自动刷新把正在输入的文本冲掉 = 手打的 Key 白丢。
这三条用真后端反而难摆（要造断链、要造半输入的焦点），用桩能钉死。
真后端那一头由 `test_pages.py` 的外壳接线与 `test_game_flow_e2e.py` 覆盖，
KataGo 真断链那一条在 `test_settings_katago.py`（只有要求 KataGo 的那一支才跑）。

FakeApi 的写法照抄 `test_review_flow.py`（回包延到下一轮事件循环，理由见那里）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from core import paths
from core.api import ApiError, Reply
from core.sound import SOUND_NAMES
from ui import theme
from ui.pages import settings as st
from tests import harness as H

# ---------------------------------------------------------------- 桩

STATUS_OK = {
    "engine": {
        "active": "katago", "warming": False,
        "katago": {"available": True, "error": "", "binary": r"D:\AI\围棋\backend\katago\katago.exe",
                   "model": r"D:\AI\围棋\backend\katago\models\kata_b18c384nbt-humanv0.bin.gz",
                   "humanModel": r"D:\AI\围棋\backend\katago\models\kata_human5k.bin.gz",
                   "stderrLog": r"D:\AI\围棋\backend\data\logs\katago.stderr.log",
                   "deathCount": 0, "lastDeathAt": "", "lastDeathReason": ""},
        "fallback": {"name": "heuristic", "available": True, "error": ""},
        "recover": {"watching": True, "attempt": 0, "maxAttempts": 5},
    },
    "llm": {"configured": False, "model": "", "baseUrl": "", "hasApiKey": False},
    "resign": {"consecutiveMoves": 8, "scoreThreshold": 30.0, "winrateThreshold": 0.05},
    "review": {"visits": 1200, "thresholds": {"slow": 1.5, "bad": 3.5, "blunder": 6.0}},
    "demotion": {"globalEnabled": True, "userEnabled": False, "losingStreak": 4},
}


def _katago(**over) -> dict:
    kat = dict(STATUS_OK["engine"]["katago"])
    kat.update(over)
    return kat


def _engine(kat=None, active="katago", warming=False, recover=None) -> dict:
    eng = {"active": active, "warming": warming, "katago": kat or _katago(),
           "recover": recover if recover is not None else dict(STATUS_OK["engine"]["recover"])}
    return eng


def status_payload(engine=None, **over) -> dict:
    data = dict(STATUS_OK)
    if engine is not None:
        data["engine"] = engine
    data.update(over)
    return data


def me_payload(**over) -> dict:
    """`/api/auth/me` 的回包。默认：已配好 baseUrl/model 与一个 Key。"""
    user = {"id": 7, "username": "stu", "displayName": "学员",
            "hintMode": True, "demotionEnabled": False,
            "llmConfig": {"baseUrl": "https://api.deepseek.com/v1",
                          "model": "deepseek-chat", "hasApiKey": True},
            "progress": {"rankId": 12, "rankName": "12级", "rankWins": 3,
                         "winsRequired": 5, "inPromotion": False}}
    user.update(over)
    return {"user": user}


class FakeApi:
    """按 (动词, 路径) 查表的假客户端，并记下每次写的包体。

    比 `test_review_flow.py` 那个多记一份 `bodies`：这一页的验收要看的是
    **发出去的 JSON**（apiKey 该不该在里面、清除是不是空串），只看路径不够。
    """

    def __init__(self, replies=None):
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[tuple[str, str, dict]] = []
        self.replies = dict(replies or {})

    def _reply(self, verb, path, body=None):
        self.calls.append((verb, path))
        if body is not None:
            self.bodies.append((verb, path, body))
        r = Reply()
        data, error = self.replies.get((verb, path),
                                       (None, ApiError(f"没有这个桩：{verb} {path}", 404)))
        QTimer.singleShot(0, lambda: r.finished.emit(data, error))
        self.replies.setdefault(("_keep", id(r)), (r, None))   # 投递前别被析构
        return r

    def get(self, path, query=None, timeout=20.0):
        return self._reply("GET", path)

    def post(self, path, body=None, query=None, timeout=20.0):
        return self._reply("POST", path, body)

    def patch(self, path, body=None, timeout=20.0):
        return self._reply("PATCH", path, body)

    def put(self, path, body=None, timeout=20.0):
        return self._reply("PUT", path, body)

    def count(self, verb, path) -> int:
        return sum(1 for c in self.calls if c == (verb, path))

    def last_body(self, verb, path) -> dict:
        for v, p, b in reversed(self.bodies):
            if v == verb and p == path:
                return b
        raise AssertionError(f"没有发过 {verb} {path}；实际调用：{self.calls}")


class FakePrefs:
    """只要这四个成员。不碰真 AppData（真 Prefs 会把开发者的登录与音量顶掉）。"""

    def __init__(self, enabled=True, volume=0.7):
        self.sound_enabled = enabled
        self.volume = volume
        self.syncs = 0

    def sync(self):
        self.syncs += 1


class FakeSound:
    def __init__(self, sounds_dir=None):
        self.played: list[str] = []
        self.sounds_dir = Path(sounds_dir) if sounds_dir else paths.SOUNDS_DIR

    def play(self, name):
        assert name in SOUND_NAMES, f"界面上摆了一个不存在的音效名：{name}"
        self.played.append(name)
        return True


class FakeInstaller(QObject):
    """假安装器。接口面与 `InstallRunner` 一致：两个信号 + start/stop。"""

    output = Signal(str)
    finished = Signal(int, str)

    def __init__(self):
        super().__init__()
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    @property
    def running(self):
        return self.started > self.stopped


@pytest.fixture
def page(qapp):
    """一页一个，用完就收。

    收尾不是洁癖：这一页会造 `InstallRunner`（里面是真 QProcess），还挂着一堆
    `QTimer.singleShot`。写法照 `test_review_flow.py` 的 page 夹具。
    （本支曾有一次 31 项后 0xC0000409 硬崩，罪不在收尾，见下面 `keyClicks` 那条注释。）
    """
    w = build(qapp)
    yield w
    w.shutdown()
    w.close()
    w.deleteLater()
    qapp.processEvents()


def build(qapp, api=None, prefs=None, sound=None):
    """建一页并等到两个 GET 都回来。"""
    w = st.SettingsPage(api or FakeApi({("GET", "/api/system/status"): (status_payload(), None),
                                        ("GET", "/api/auth/me"): (me_payload(), None)}),
                        sound or FakeSound(),
                        prefs or FakePrefs())
    w.resize(1104, 760)            # 真实外壳的内容宽，见 test_review_flow 同处注释
    w.show()
    qapp.processEvents()
    w.refresh()
    assert H.wait(qapp, lambda: w._status and w._user), "两个 GET 没都回来"
    return w


# ---------------------------------------------------------------- 引擎四态：纯函数

def test_state_ready_when_katago_is_available():
    assert st.engine_state(_engine()) == "ready"


def test_state_warming_is_its_own_state():
    """预热单独一态：否则刚起的服务看着像没装引擎（网页版先定下的口径）。"""
    eng = _engine(kat=_katago(available=False, warming=True), active="heuristic", warming=True)
    assert st.engine_state(eng) == "warming"


def test_state_recovering_while_the_watchdog_is_trying():
    eng = _engine(kat=_katago(available=False, deathCount=2, active="heuristic"),
                  active="heuristic",
                  recover={"watching": True, "attempt": 2, "maxAttempts": 5})
    assert st.engine_state(eng) == "recovering"


def test_state_stalled_once_the_watchdog_gave_up():
    eng = _engine(kat=_katago(available=False, deathCount=9), active="heuristic",
                  recover={"watching": False, "attempt": 5, "maxAttempts": 5})
    assert st.engine_state(eng) == "stalled"


def test_state_missing_when_it_was_never_installed():
    eng = _engine(kat=_katago(available=False, error="没找到 katago.exe"), active="heuristic")
    assert st.engine_state(eng) == "missing"


def test_an_engine_that_already_healed_is_not_reported_as_broken():
    """**本页最容易说错话的一处**。

    `deathCount` 是历史计数，看门狗把引擎拉起来之后它不会归零。照网页版只看
    `deathCount > 0` 就会在一台完全正常的机器上永远挂着「正在自动重启」。
    所以 `available` 必须先问 —— 已恢复就是就绪，断链只作为补充说明出现。
    """
    eng = _engine(kat=_katago(available=True, deathCount=3,
                              lastDeathAt="2026-09-07 10:11:12"))
    assert st.engine_state(eng) == "ready"
    head, tail = st.engine_detail(eng)
    assert "自愈 3 次" in tail, tail
    assert "正在自动重启" not in head + tail


def test_watching_is_not_evidence_that_the_engine_is_broken():
    """`recover.watching` 的实现是「看门狗任务还在」—— 装了 KataGo 的机器上恒为 True。

    它只能用来在**已经断链**之后区分「还在试」与「已放弃」，不能当故障信号用。
    这一条钉的是下一个读这个字段的人（包括我）会犯的错。
    """
    eng = _engine()                      # available=True 且 watching=True
    assert eng["recover"]["watching"] is True
    assert st.engine_state(eng) == "ready"


def test_an_old_backend_without_the_new_fields_still_gets_an_answer():
    """`warming`/`deathCount`/`recover` 是后端后加的字段。缺了不许 KeyError，
    也不许把「没字段」读成「在自愈」。"""
    assert st.engine_state({"active": "heuristic"}) == "missing"
    assert st.engine_state({"active": "katago",
                            "katago": {"available": True}}) == "ready"
    head, tail = st.engine_detail({"active": "heuristic"})
    assert "内置启发式" in head, head


# ---------------------------------------------------------------- 引擎四态：界面措辞

def set_status(qapp, w, engine):
    """把一份 engine 段喂进界面并等绘制完成。"""
    w._status = status_payload(engine=engine)
    w._paint_engine()
    qapp.processEvents()
    return w.engineBadge.text()


@pytest.mark.parametrize("state,want", [
    ("ready", "KataGo 就绪"),
    ("warming", "KataGo 预热中"),
    ("recovering", "断链自愈中"),
    ("stalled", "自动重启已停止"),
    ("missing", "未安装 KataGo"),
])
def test_the_badge_word_follows_the_state(qapp, page, state, want):
    """四态在界面上必须有四个不同的说法，而且徽章颜色要跟着分得开。"""
    cases = {
        "ready": _engine(),
        "warming": _engine(kat=_katago(available=False), active="heuristic", warming=True),
        "recovering": _engine(kat=_katago(available=False, deathCount=2), active="heuristic"),
        "stalled": _engine(kat=_katago(available=False, deathCount=7), active="heuristic",
                           recover={"watching": False, "attempt": 5, "maxAttempts": 5}),
        "missing": _engine(kat=_katago(available=False, error="没找到 katago.exe"),
                           active="heuristic"),
    }
    got = set_status(qapp, page, cases[state])
    assert got == want, f"{state} 态的徽章是「{got}」"
    assert page.engineDetail.text()


def test_a_never_installed_engine_does_not_shout_in_red(qapp, page):
    """没装 KataGo 是**支持过的部署**（降级仍可用），不该用错误色让人以为程序坏了。

    这一处主动偏离网页版（那里是 `color:#c92a2a` 的红字），记在模块文档字符串。
    """
    set_status(qapp, page, _engine(kat=_katago(available=False, error="没找到 katago.exe"),
                                   active="heuristic"))
    sheet = page.engineBadge.styleSheet()
    # 拿主题表比而不是拿字面色值比：颜色调一次就要追改一遍测试，那种测试护不住口径
    assert theme.BADGE_COLORS["warn"][1] in sheet, sheet
    assert theme.BADGE_COLORS["err"][1] not in sheet, "未装态用了错误色：" + sheet


def test_the_stalled_line_says_what_to_do_next_without_a_log_path(qapp, page):
    """卡死态要给人「下一步做什么」，但不再把 stderr 路径摆上屏（第 33 轮起）。

    老文案把本机日志路径写进界面 —— 那是部署细节；学员只需要知道
    「重启应用试试 + 不行就把日志发给支持」。
    """
    set_status(qapp, page, _engine(kat=_katago(available=False, deathCount=7,
                                               error="进程退出", lastDeathAt="2026-09-07 09:00:00"),
                                   active="heuristic",
                                   recover={"watching": False, "attempt": 5, "maxAttempts": 5}))
    assert "断链 7 次" in page.engineDetail.text(), page.engineDetail.text()
    assert "2026-09-07 09:00:00" in page.engineDetail.text()
    hint = page.engineHint.text()
    assert "重启应用" in hint, hint              # 交给人看的下一步
    assert "不必重装" in hint, hint
    assert ".log" not in hint and "\\" not in hint, f"界面不该出现日志路径：{hint}"
    assert page.engineHint.isVisible()


def test_recovering_shows_which_attempt_this_is(qapp, page):
    set_status(qapp, page, _engine(kat=_katago(available=False, deathCount=2),
                                   active="heuristic",
                                   recover={"watching": True, "attempt": 3, "maxAttempts": 5}))
    assert "第 3/5 次" in page.engineHint.text(), page.engineHint.text()


def test_the_install_button_only_shows_where_it_can_help(qapp, page):
    """就绪 / 预热 / 自愈中都不该给一个「再下 90MB」的按钮；未装与放弃自愈才给。"""
    for engine, want in (
        (_engine(), False),
        (_engine(kat=_katago(available=False), active="heuristic", warming=True), False),
        (_engine(kat=_katago(available=False, deathCount=1), active="heuristic"), False),
        (_engine(kat=_katago(available=False, error="x"), active="heuristic"), True),
        (_engine(kat=_katago(available=False, deathCount=7), active="heuristic",
                 recover={"watching": False, "attempt": 5, "maxAttempts": 5}), True),
    ):
        set_status(qapp, page, engine)
        assert page.btnInstall.isVisibleTo(page) is want, \
            f"态 {st.engine_state(engine)} 的按钮可见性应当是 {want}"


def test_the_engine_card_tells_state_not_paths(qapp, page):
    """引擎卡给学员的是「现在用什么、能不能用」，不是本机文件路径。

    引擎路径/权重路径是部署细节（绝对路径 + 文件后缀），第 33 轮起不摆上屏。
    这一条顺带钉死「当前引擎」仍然从接口取，不许写死成某个名字。
    """
    set_status(qapp, page, _engine())
    assert page.kv["当前引擎"].fullText() == "KataGo", page.kv["当前引擎"].text()
    assert "就绪" in page.engineDetail.text(), page.engineDetail.text()
    # 页面不该出现磁盘路径：路径分隔符 / 反斜杠只可能是开发者痕迹
    visible = (page.engineDetail.text() + page.engineHint.text()
               + page.rulesLine.text() + page.accuracyLine.text())
    assert "\\" not in visible and not visible.rstrip().endswith(".bin.gz"), visible

    # 未装那一态也要有人话说明（「安装方法」在 engineHint 里）
    set_status(qapp, page, _engine(kat=_katago(available=False, error=""), active="heuristic"))
    assert "未检测到可用的 KataGo" in page.engineDetail.text(), page.engineDetail.text()


# ---------------------------------------------------------------- 规则参数文案

def test_resign_and_review_lines_keep_the_units_and_the_order(qapp, page):
    """认输那条要「连续 N 手 / <百分比 / 落后 M 目」三件齐；复盘那条要三个阈值齐。

    口径抄网页版：胜率阈值是 0-1 的小数，写给学员看必须换成百分比。
    """
    set_status(qapp, page, _engine())
    rules = page.rulesLine.text()
    assert "连续 8 手" in rules and "<5%" in rules and "落后 30 目" in rules, rules
    acc = page.accuracyLine.text()
    assert "1200 次推演" in acc, acc
    assert acc.index("缓手") < acc.index("恶手") < acc.index("大恶手"), acc
    assert "1.5 目" in acc and "3.5 目" in acc and "6 目" in acc, acc


def test_the_demotion_label_uses_the_number_the_server_chose():
    """连败几场降级是后端配置（`GO_DEMOTION_LOSING_STREAK`），不许写死在界面上。"""
    d = st.demotion_text({"losingStreak": 6})
    assert "6 场" in d, d
    assert "4" not in d.replace("6", ""), d            # 默认值 4 不该漏进来
    assert "4 场" in st.demotion_text({}), "字段缺位时退回默认 4 场"


def test_volume_percent_does_not_leak_a_float_tail():
    assert st.volume_percent(0.075) == 8
    assert st.volume_percent(1.0) == 100
    assert st.volume_percent(-2) == 0
    assert st.volume_percent(9) == 100


# ---------------------------------------------------------------- 大模型：读

def test_llm_fields_are_filled_from_the_account(qapp):
    w = build(qapp, api=FakeApi({("GET", "/api/system/status"): (status_payload(), None),
                                 ("GET", "/api/auth/me"): (me_payload(), None)}))
    assert w.edBaseUrl.text() == "https://api.deepseek.com/v1"
    assert w.edModel.text() == "deepseek-chat"
    assert w.keyBadge.isVisibleTo(w) and w.btnClearKey.isVisibleTo(w)


def test_the_api_key_is_never_echoed_back(qapp):
    """后端只回 `hasApiKey`，界面上那一栏必须始终是空的。

    万一哪天有人「顺手」把 key 回填进去，明文就会出现在一个没有密码保护的
    窗口标题栏附近（截图、远程协助、录屏都看得见）。
    """
    w = build(qapp, api=FakeApi({("GET", "/api/system/status"): (status_payload(), None),
                                 ("GET", "/api/auth/me"): (me_payload(), None)}))
    assert w.edApiKey.text() == ""
    assert w.edApiKey.echoMode() == QLineEdit.EchoMode.Password
    assert "留空表示不修改" in w.edApiKey.placeholderText()


def test_no_key_yet_means_no_badge_no_clear_button(qapp):
    w = build(qapp, api=FakeApi({("GET", "/api/system/status"): (status_payload(), None),
                                 ("GET", "/api/auth/me"): (me_payload(
                                     llmConfig={"baseUrl": "", "model": "",
                                                "hasApiKey": False}), None)}))
    assert not w.keyBadge.isVisibleTo(w)
    assert not w.btnClearKey.isVisibleTo(w)
    assert w.edApiKey.placeholderText() == "sk-…"


def test_a_failed_status_read_says_so_and_still_shows_the_rest(qapp):
    """两个 GET 各管一半：读不到引擎不该把 LLM 配置一起挡住。"""
    api = FakeApi({("GET", "/api/system/status"): (None, ApiError("后端 500", 500)),
                   ("GET", "/api/auth/me"): (me_payload(), None)})
    w = st.SettingsPage(api, FakeSound(), FakePrefs())
    w.show()
    qapp.processEvents()
    w.refresh()
    assert H.wait(qapp, lambda: w._user and w.errorBar.label.text())
    assert "500" in w.errorBar.label.text(), w.errorBar.label.text()
    assert w.edModel.text() == "deepseek-chat", "引擎读失败不该连累大模型那一半"
    assert w.engineBadge.text() == "—"


# ---------------------------------------------------------------- 大模型：写

def click(qapp, button):
    QTest.mouseClick(button, Qt.LeftButton)
    qapp.processEvents()


def test_save_sends_base_url_and_model_and_leaves_the_key_out(qapp):
    """**没填 Key 就不许发 apiKey**：后端把「传了空串」当成清除。

    写错这一条的症状是：改了模型名点保存，原来配好的 Key 没了，
    复盘静默降级成模板讲解 —— 用户完全看不出因果。
    """
    api = FakeApi({("GET", "/api/system/status"): (status_payload(), None),
                   ("GET", "/api/auth/me"): (me_payload(), None),
                   ("PUT", "/api/auth/me/llm"): ({"llmConfig": {
                       "baseUrl": "https://api.deepseek.com/v1",
                       "model": "qwen-plus", "hasApiKey": True}}, None)})
    w = st.SettingsPage(api, FakeSound(), FakePrefs())
    w.show()
    qapp.processEvents()
    w.refresh()
    assert H.wait(qapp, lambda: w._user)
    QTest.keyClicks(w.edModel, "-plus")           # 直接在原值后面续，模拟真实改法
    click(qapp, w.btnSaveLlm)
    assert H.wait(qapp, lambda: api.count("PUT", "/api/auth/me/llm"))
    assert api.last_body("PUT", "/api/auth/me/llm") == {
        "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat-plus"}
    assert "apiKey" not in api.last_body("PUT", "/api/auth/me/llm")
    assert H.wait(qapp, lambda: "已保存" in w.msg.label.text()), w.msg.label.text()
    assert w.msg.kind == "ok"
    # 保存完展示的是**服务端存下来的值**（上面那个桩故意回了个不同的模型名）。
    # 跟网页版一致：那边 saveLLM 之后 auth store 被回包顶掉，`useEffect([user])`
    # 会把三个输入框重新按服务端值填一遍。后端会 trim / 规范化，界面继续显示
    # 用户手打的草稿就是在骗人。
    assert w.edModel.text() == "qwen-plus", w.edModel.text()
    assert not w.edModel.isModified(), "回填不能算用户改动，否则下一次刷新就永远不跟服务走了"
    assert w.edApiKey.text() == "", "保存成功后明文 Key 不许留在窗口里（截图、录屏都会拍到）"
    assert w.btnSaveLlm.isEnabled(), "保存完得能接着再点"


def test_typing_a_key_sends_it_once_and_the_field_is_cleared_after(qapp):
    api = FakeApi({("GET", "/api/system/status"): (status_payload(), None),
                   ("GET", "/api/auth/me"): (me_payload(
                       llmConfig={"baseUrl": "", "model": "", "hasApiKey": False}), None),
                   ("PUT", "/api/auth/me/llm"): ({"llmConfig": {
                       "baseUrl": "", "model": "", "hasApiKey": True}}, None)})
    w = st.SettingsPage(api, FakeSound(), FakePrefs())
    w.show()
    qapp.processEvents()
    w.refresh()
    assert H.wait(qapp, lambda: w._user)
    QTest.keyClicks(w.edApiKey, "sk-abcdef")
    click(qapp, w.btnSaveLlm)
    assert H.wait(qapp, lambda: api.count("PUT", "/api/auth/me/llm"))
    assert api.last_body("PUT", "/api/auth/me/llm")["apiKey"] == "sk-abcdef"
    # 保存成功后 Key 必须从界面上消失（接口已回 hasApiKey=True），
    # 否则它会一直挂在输入框里，截图与录屏都是明文泄漏面
    assert H.wait(qapp, lambda: w.edApiKey.text() == "" and w.keyBadge.isVisibleTo(w))


def test_clear_key_sends_an_empty_string(qapp):
    """清除 = 发空串，这是后端定的协议（`if body.apiKey is not None` 那一段）。"""
    api = FakeApi({("GET", "/api/system/status"): (status_payload(), None),
                   ("GET", "/api/auth/me"): (me_payload(), None),
                   ("PUT", "/api/auth/me/llm"): ({"llmConfig": {
                       "baseUrl": "https://api.deepseek.com/v1",
                       "model": "deepseek-chat", "hasApiKey": False}}, None)})
    w = st.SettingsPage(api, FakeSound(), FakePrefs())
    w.show()
    qapp.processEvents()
    w.refresh()
    assert H.wait(qapp, lambda: w.btnClearKey.isVisibleTo(w))
    click(qapp, w.btnClearKey)
    assert api.last_body("PUT", "/api/auth/me/llm") == {"apiKey": ""}
    assert H.wait(qapp, lambda: "模板讲解" in w.msg.label.text()), w.msg.label.text()
    assert H.wait(qapp, lambda: not w.btnClearKey.isVisibleTo(w))


def test_a_failed_save_does_not_look_like_a_success(qapp):
    api = FakeApi({("GET", "/api/system/status"): (status_payload(), None),
                   ("GET", "/api/auth/me"): (me_payload(), None)})   # PUT 没有桩 → 404
    w = st.SettingsPage(api, FakeSound(), FakePrefs())
    w.show()
    qapp.processEvents()
    w.refresh()
    assert H.wait(qapp, lambda: w._user)
    click(qapp, w.btnSaveLlm)
    assert H.wait(qapp, lambda: w.msg.isVisibleTo(w))
    assert w.msg.kind == "err", w.msg.kind
    assert "没有这个桩" in w.msg.label.text()
    assert w.btnSaveLlm.isEnabled(), "失败后按钮要能再点，卡成禁用就是死路"


def test_the_connection_test_shows_the_servers_own_words(qapp):
    ok = {"ok": True, "model": "deepseek-chat", "message": "连接成功（deepseek-chat），模型回复：你好"}
    api = FakeApi({("GET", "/api/system/status"): (status_payload(), None),
                   ("GET", "/api/auth/me"): (me_payload(), None),
                   ("POST", "/api/system/llm-test"): (ok, None)})
    w = st.SettingsPage(api, FakeSound(), FakePrefs())
    w.show()
    qapp.processEvents()
    w.refresh()
    assert H.wait(qapp, lambda: w._user)
    click(qapp, w.btnTestLlm)
    assert H.wait(qapp, lambda: "模型回复" in w.msg.label.text()), w.msg.label.text()
    assert w.msg.kind == "ok"
    assert api.count("POST", "/api/system/llm-test") == 1


def test_a_failed_connection_test_uses_the_error_colour(qapp):
    bad = {"ok": False, "message": "连接失败：401 Unauthorized"}
    api = FakeApi({("GET", "/api/system/status"): (status_payload(), None),
                   ("GET", "/api/auth/me"): (me_payload(), None),
                   ("POST", "/api/system/llm-test"): (bad, None)})
    w = st.SettingsPage(api, FakeSound(), FakePrefs())
    w.show()
    qapp.processEvents()
    w.refresh()
    assert H.wait(qapp, lambda: w._user)
    click(qapp, w.btnTestLlm)
    assert H.wait(qapp, lambda: w.msg.isVisibleTo(w))
    assert w.msg.kind == "err", w.msg.kind
    assert "401" in w.msg.label.text()


def test_an_unsaved_draft_survives_a_refresh(qapp):
    """自动刷新（安装完的收尾、重新进入本页）不许把手正在打的字冲掉。

    判据用 `isModified()` 而不是 `hasFocus()`：焦点会在点「保存」的那一刻离开输入框，
    而 `isModified` 正好是「这栏被人动过」的 Qt 原生账。offscreen 平台下
    `hasFocus()` 根本拿不到 True，用焦点当判据这条测试就没法写。
    """
    api = FakeApi({("GET", "/api/system/status"): (status_payload(), None),
                   ("GET", "/api/auth/me"): (me_payload(), None)})
    w = st.SettingsPage(api, FakeSound(), FakePrefs())
    w.show()
    qapp.processEvents()
    w.refresh()
    assert H.wait(qapp, lambda: w._user)
    # 只能打 ASCII：`QTest.keyClicks` 送中文会让整个进程 0xC0000409 硬崩
    # （PySide6 6.11.2 + offscreen，已实测；投 QInputMethodEvent 也不生效）。
    QTest.keyClicks(w.edBaseUrl, "/my-gateway")
    assert w.edBaseUrl.isModified(), "真打字必须把『用户动过』记上，下面的断言才有前提"
    assert not w.edModel.isModified(), "回填不算动过：这条不成立，下面那半截就不算数"
    w._on_me(me_payload(llmConfig={"baseUrl": "https://别的/v1", "model": "x",
                                   "hasApiKey": True}), None)
    assert "/my-gateway" in w.edBaseUrl.text(), "改过的字段被刷新冲掉了"
    assert w.edModel.text() == "x", "没动过的字段应当跟着接口走"
    # 保存一次之后账要清零，否则改过一回就永远不再与服务端同步了
    w.edBaseUrl.setModified(False)
    w._on_me(me_payload(llmConfig={"baseUrl": "https://又换了/v1", "model": "x",
                                   "hasApiKey": True}), None)
    assert w.edBaseUrl.text() == "https://又换了/v1"


# ---------------------------------------------------------------- 教学偏好

def test_ticking_a_preference_patches_the_server(qapp):
    back = {"user": {**me_payload()["user"], "hintMode": False}}
    api = FakeApi({("GET", "/api/system/status"): (status_payload(), None),
                   ("GET", "/api/auth/me"): (me_payload(), None),
                   ("PATCH", "/api/auth/me"): (back, None)})
    w = st.SettingsPage(api, FakeSound(), FakePrefs())
    w.show()
    qapp.processEvents()
    w.refresh()
    assert H.wait(qapp, lambda: w.chkHint.isChecked())
    click(qapp, w.chkHint)
    assert not w.chkHint.isChecked(), "点下去就该先反映用户意图，回包再来纠正"
    assert api.last_body("PATCH", "/api/auth/me") == {"hintMode": False}
    assert H.wait(qapp, lambda: w._user.get("hintMode") is False), w._user
    assert w.chkHint.isEnabled()


def test_a_refused_preference_bounces_the_box_back(qapp):
    """用户点的那一下只是请求，回包说了算。

    拿“服务端把 hintMode 原值 True 回回来”（相当于拒了这次修改）验：
    不回填的话界面会一直显示“已关闭”，而服务端的对局页还在给推荐点。
    """
    api = FakeApi({("GET", "/api/system/status"): (status_payload(), None),
                   ("GET", "/api/auth/me"): (me_payload(), None),
                   ("PATCH", "/api/auth/me"): (me_payload(), None)})   # 回包仍是 True
    w = st.SettingsPage(api, FakeSound(), FakePrefs())
    w.show()
    qapp.processEvents()
    w.refresh()
    assert H.wait(qapp, lambda: w.chkHint.isChecked())
    click(qapp, w.chkHint)
    assert H.wait(qapp, lambda: w.chkHint.isChecked()), \
        "服务端说没关掉，界面就得自己摆回去"
    assert not [c for c in api.calls if c[0] == "PATCH"][1:], "回填又把 PATCH 发了一遍"


def test_the_backfill_itself_does_not_write_back(qapp):
    """回填时必须拦掉 toggled，否则一进页面就自己发一次 PATCH（并把开关顶反）。

    这条是「界面自己写自己」那个环的保险：PATCH 的回包会再触发一次回填。
    """
    api = FakeApi({("GET", "/api/system/status"): (status_payload(), None),
                   ("GET", "/api/auth/me"): (me_payload(demotionEnabled=True), None)})
    w = st.SettingsPage(api, FakeSound(), FakePrefs())
    w.show()
    qapp.processEvents()
    w.refresh()
    assert H.wait(qapp, lambda: w.chkDemotion.isChecked())
    qapp.processEvents()
    assert not [c for c in api.calls if c[0] == "PATCH"], api.calls


def test_no_account_yet_means_no_write(qapp):
    """还没读到账号时开关是空的，这时候发 PATCH 会把默认值写成用户的偏好。"""
    api = FakeApi({("GET", "/api/system/status"): (status_payload(), None)})
    w = st.SettingsPage(api, FakeSound(), FakePrefs())
    w.show()
    qapp.processEvents()
    w.chkHint.setChecked(True)
    qapp.processEvents()
    assert not [c for c in api.calls if c[0] == "PATCH"], api.calls


# ---------------------------------------------------------------- 音效

def test_the_sound_settings_live_on_the_machine(qapp):
    """开关与音量写 `Prefs`（本机），不发 PATCH（跟账号走）。

    换台电脑音量不该跟着登录态跑过来；这条形状是网页版先定的
    （它存 localStorage，同一个理由），两边一致才不会出现
    「网页上静音了、桌面上还在响」。"""
    prefs = FakePrefs(enabled=True, volume=0.7)
    w = build(qapp, prefs=prefs)
    assert w.chkSound.isChecked(), "没把本机偏好读上来"
    assert w.sldVolume.value() == 70 and w.volLabel.text() == "70%"
    before = list(w._api.calls)
    w.chkSound.setChecked(False)
    qapp.processEvents()
    assert prefs.sound_enabled is False
    assert not [c for c in w._api.calls if c[0] in ("PATCH", "PUT", "POST")], w._api.calls
    assert before


def test_turning_sound_off_disables_the_preview_buttons(qapp):
    """关了音效就不该有按钮能“抱一下运气”：试听了没声音会被当成缺陷报。"""
    w = build(qapp, prefs=FakePrefs(enabled=False))
    assert not w.chkSound.isChecked()
    assert all(not b.isEnabled() for b in w.btnPreview.values()), "关着却还能点"
    w.chkSound.setChecked(True)
    qapp.processEvents()
    assert all(b.isEnabled() for b in w.btnPreview.values())
    assert w._sound.played == ["click"], w._sound.played   # 一打开先给一声


def test_the_slider_writes_the_volume_and_the_label(qapp):
    prefs = FakePrefs()
    w = build(qapp, prefs=prefs)
    w.sldVolume.setValue(45)
    qapp.processEvents()
    assert abs(prefs.volume - 0.45) < 1e-9, prefs.volume
    assert w.volLabel.text() == "45%"
    assert prefs.syncs == 0, "拖动过程中不该每次落盘（同步只在松手时）"
    assert w._sound.played == [], w._sound.played           # 拖动不出声


def test_releasing_the_slider_plays_once_and_syncs(qapp):
    prefs = FakePrefs()
    w = build(qapp, prefs=prefs)
    w.sldVolume.setValue(30)
    w.sldVolume.sliderReleased.emit()
    qapp.processEvents()
    assert prefs.syncs == 1, "松手没落盘：关掉窗口会丢这次调整"
    assert w._sound.played == ["click"], w._sound.played


def test_missing_wav_files_are_reported_on_entry(tmp_path, qapp):
    """资源不全必须一进来就说，不能等用户点了试听才发现某一声从来没响过。"""
    for name in ("stone", "capture", "win"):
        (tmp_path / f"{name}.wav").write_bytes(b"RIFF")
    w = build(qapp, sound=FakeSound(tmp_path))
    lack = st.missing_sounds(tmp_path)
    assert len(lack) == len(SOUND_NAMES) - 3, lack
    assert w.lackBar.isVisibleTo(w)
    assert "缺 10 个音效文件" in w.lackBar.label.text(), w.lackBar.label.text()
    assert "wrong" in w.lackBar.label.text()          # 报出缺的是哪几个


def test_the_bundled_resources_are_complete(qapp):
    """随包的 13 个 wav 一个都不能缺（缺了就是仓库里被人误删）。"""
    assert st.missing_sounds(paths.SOUNDS_DIR) == ()
    w = build(qapp)
    assert not w.lackBar.isVisibleTo(w)


def test_every_preview_button_names_a_real_sound(qapp):
    w = build(qapp)
    for name, btn in w.btnPreview.items():
        assert name in SOUND_NAMES
        click(qapp, btn)
        assert w._sound.played[-1] == name, f"「{btn.text()}」放的是 {w._sound.played[-1]}"


# ---------------------------------------------------------------- 安装 KataGo

def test_the_runner_points_at_the_download_script(qapp):
    """不跑它，只验“要跑什么”：命令错一个字符就会去下一个没用的东西。"""
    assert (paths.BACKEND_DIR / "katago" / "download.py").exists(), paths.BACKEND_DIR
    r = st.InstallRunner()
    qapp.processEvents()
    assert r.proc.program() == sys.executable
    assert r.proc.arguments() == ["katago/download.py"]
    assert Path(r.proc.workingDirectory()) == paths.BACKEND_DIR
    env = r.proc.processEnvironment()
    assert env.value("PYTHONUTF8") == "1", "不设 UTF-8 子进程的中文进度会变问号"
    assert env.value("PYTHONUNBUFFERED") == "1", "缓冲了就不是“一点点冒出来”而是一刷到底"
    assert not r.running, "光是构造不能把下载脚本带起来（一点安装就跑两次是事故）"


def missing_api(**extra):
    """一份「未安装 KataGo」的 status 桩 —— 只有那一态才给安装按钮。"""
    eng = _engine(kat=_katago(available=False, error="没找到 katago.exe"),
                  active="heuristic")
    replies = {("GET", "/api/system/status"): (status_payload(engine=eng), None),
               ("GET", "/api/auth/me"): (me_payload(), None)}
    replies.update(extra)
    return FakeApi(replies)


def install_page(qapp, api=None):
    """一页处于未装态、并已接上假安装器的页面。"""
    w = build(qapp, api=api or missing_api())
    runner = FakeInstaller()
    w._make_installer = lambda: runner
    return w, runner


def test_clicking_install_starts_the_runner_and_opens_the_log(qapp):
    w, runner = install_page(qapp)
    assert w.btnInstall.isVisibleTo(w)
    assert not w.installLog.isVisibleTo(w)
    click(qapp, w.btnInstall)
    assert runner.started == 1
    assert w.installLog.isVisibleTo(w)
    assert "[安装已开始]" in w.installLog.toPlainText()
    assert w.btnStop.isVisibleTo(w) and not w.btnInstall.isVisibleTo(w)
    assert not w.btnReload.isEnabled(), "装到一半不该能手动刷新（会把按钮可见性改乱）"


def test_clicking_install_twice_does_not_start_two_runners(qapp):
    """两个 download.py 同时写同一个目录：轻则文件互相顶，重则留一个半截的 exe。"""
    w, runner = install_page(qapp)
    click(qapp, w.btnInstall)
    click(qapp, w.btnInstall)
    click(qapp, w.btnInstall)
    assert runner.started == 1


def test_an_install_in_progress_does_not_offer_a_second_install(qapp):
    """装到一半切走再切回来：不能同时出现「安装 KataGo」与「停止」。截图 p5_03 抓的。

    `refresh()` 会在每次进本页时重画引擎那一栏，而那时状态还是 `missing`
    （引擎还没装完）—— 不拿 `installer` 挡一下，按钮位就会跳回「安装 KataGo」，
    而那句「点下面的『安装 KataGo』」也一起回来指着一个不在的按钮。
    安装中的文案必须跟着换（`INSTALLING_HINT`）。
    """
    w, runner = install_page(qapp)
    click(qapp, w.btnInstall)
    assert w.installTip.isVisibleTo(w), "安装中得告诉学员进度在哪看"
    assert "正在安装" in w.installTip.text(), w.installTip.text()
    assert "点下面的" not in w.installTip.text(), "文案还在指一个已经不在的按钮"
    # 同样指错地方的还有状态下面那行补充说明（未装态的 `engine_detail` 尾巴）：
    # 它不在 `installTip` 里，看截图才看得见（p5_03 第一版就是两句同时挂在那）。
    assert "点下面的" not in w.engineHint.text(), w.engineHint.text()
    assert not w.engineHint.isVisibleTo(w), "安装中不该再留一句「安装方法」"

    w.refresh()                        # 模拟「切回本页」（外壳每次切页都这么干）
    assert H.wait(qapp, lambda: w.btnStop.isVisibleTo(w))
    assert not w.btnInstall.isVisibleTo(w), "装到一半又给一个安装入口"
    assert "正在安装" in w.installTip.text(), w.installTip.text()

    runner.finished.emit(0, "")        # 跑完得把按钮位交还回去
    assert w.btnInstall.isVisibleTo(w) and not w.btnStop.isVisibleTo(w)
    assert "正在安装" not in w.installTip.text(), "装完了还在报安装中"
    assert "download.py" in w.installTip.text(), w.installTip.text()
    assert "安装方法" in w.engineHint.text(), "那行补充说明得跟着回来"


def test_install_output_lands_in_the_view_and_is_tailed(qapp):
    """进度会刷几百行，视图只留最后 `LOG_TAIL` 行。

    留头还是留尾？留尾：`download.py` 失败时原因打在最后，
    开头那些「正在下载 xxx.zip」在失败的那一刻已经没有价值了 ——
    这一条钉的就是这个取舍，别被「开头那行看不到了」骗回去改成留头。
    """
    w, runner = install_page(qapp)
    click(qapp, w.btnInstall)
    runner.output.emit("下载引擎包 windows-cuda.zip\n")
    for i in range(st.LOG_TAIL + 60):
        runner.output.emit(f"[{i:04d}] 进度")
    runner.output.emit("下载失败：连接被重置")
    qapp.processEvents()
    text = w.installLog.toPlainText()
    assert "[0000] 进度" not in text, "尾巴没裁住，视图会无限长"
    assert f"[{st.LOG_TAIL + 59:04d}] 进度" in text, "留的应该是最新的而不是中间的"
    assert "下载失败：连接被重置" in text, "最该看见的那一行被洗掉了"
    assert w.installLog.document().blockCount() <= st.LOG_TAIL


def test_a_finished_install_refreshes_the_engine_status(qapp):
    """跑完必须自己重读一次：人不该为了看看装上了没再点一下刷新。"""
    api = missing_api()
    w, runner = install_page(qapp, api=api)
    before = api.count("GET", "/api/system/status")
    click(qapp, w.btnInstall)
    runner.finished.emit(0, "")
    assert H.wait(qapp, lambda: api.count("GET", "/api/system/status") > before)
    assert w.installer is None
    assert w.btnInstall.isVisibleTo(w) and not w.btnStop.isVisibleTo(w)
    assert w.btnReload.isEnabled()
    assert "[安装完成]" in w.installLog.toPlainText()


def test_a_failed_install_says_what_to_do_next(qapp):
    api = missing_api()
    w, runner = install_page(qapp, api=api)
    before = api.count("GET", "/api/system/status")
    click(qapp, w.btnInstall)
    runner.output.emit("找不到可用的下载通道")
    runner.finished.emit(2, "Process crashed")
    qapp.processEvents()
    assert w.msg.kind == "err", w.msg.kind
    # 退出码是开发者概念：给学员的话是「没装上 + 去哪看原因 + 能重试」
    assert "安装没有完成" in w.msg.label.text(), w.msg.label.text()
    assert "退出码" not in w.msg.label.text(), w.msg.label.text()
    assert "找不到可用的下载通道" in w.installLog.toPlainText()
    assert api.count("GET", "/api/system/status") == before, "没装成就不必重读引擎"
    assert w.btnInstall.isVisibleTo(w), "失败了要能再点"


def test_stopping_and_shutdown_take_the_child_along(qapp):
    """半途关掉窗口不能留一个孤儿 download.py 继续占着网络。"""
    w, runner = install_page(qapp)
    click(qapp, w.btnInstall)
    w.shutdown()
    assert runner.stopped == 1
    assert w.installer is None
    w2, runner2 = install_page(qapp)
    click(qapp, w2.btnInstall)
    click(qapp, w2.btnStop)
    assert runner2.stopped == 1
    assert w2.installer is not None, "停止要等子进程真的退了才收尾（否则输出会漏一段）"


def test_the_default_installer_is_a_real_runner(qapp):
    """防“只测了假安装器”：真缝上必须给的是 QProcess 那个类。"""
    w = build(qapp, api=missing_api())
    runner = w._make_installer()
    assert isinstance(runner, st.InstallRunner)
    assert not runner.running
    runner.deleteLater()


# ---------------------------------------------------------------- 看图

def test_the_page_fits_the_narrowest_supported_window(qapp):
    """1280 窗口下内容视口只有 1112 宽，本页不得要更多（横向没得滚）。

    为什么在本页量而不只靠 `test_pages.py` 那个外壳验收：那条要真起后端、
    真注册，一轮七十秒；而这一条只是一句 `sizeHint`，1.7 秒就能把「新加一句
    长文案又把页顶爆了」这类回退拦在写它的人面前。
    折行标签不封顶就是 1400px（见 `WRAP_MAX`）。
    """
    w = build(qapp)
    hint = w.sizeHint()
    assert hint.width() <= 1112, f"设置页要 {hint.width()} 宽，视口只有 1112"
    assert w.minimumSize().width() <= 1112


def test_no_text_is_clipped_on_the_settings_page(qapp):
    """把“看图”机械化：没有布局静默裁字（包括未装态与自愈态文案都不许裁）。"""
    for api in (missing_api(), None):
        w = build(qapp, api=api) if api else build(qapp)
        offenders, scanned = H.clipped_texts(w)
        assert scanned >= 12, f"只扫了 {scanned} 个控件，页面八成没建起来（假绿）"
        assert not offenders, "；".join(offenders)
        w.close()
        QApplication.processEvents()


def test_screenshots_for_me_to_read(qapp):
    """三张：未装（有安装入口）/ 就绪 / 正在装。我逐张读。"""
    shots = []

    def shot(w, name):
        """截图 + 当场量“画面里有内容吗”。`blank_ratio` 要趁控件还显时量，
        关掉之后再 grab 只会量到一张空图。"""
        p = H.snap(w, name)
        ratio = H.blank_ratio(w)
        assert ratio > 0.4, f"{name} 几乎是空的（非背景像素只占 {ratio:.0%}）"
        return p

    w = build(qapp, api=missing_api())
    shots.append(shot(w, "p5_01_settings_missing"))
    w.close()

    w = build(qapp)                      # 默认桩：就绪 + 已配 Key
    QTest.keyClicks(w.edApiKey, "sk-abcdef")
    shots.append(shot(w, "p5_02_settings_ready"))
    w.close()

    api = missing_api()
    w, runner = install_page(qapp, api)
    click(qapp, w.btnInstall)
    runner.output.emit("[katago-setup] 下载引擎包 windows-cuda.zip  60.1/90.4 MB  6.2 MB/s")
    runner.output.emit("[katago-setup] 校验通过，解压到 backend/katago")
    qapp.processEvents()
    shots.append(shot(w, "p5_03_settings_installing"))
    w.shutdown()
    w.close()
    QApplication.processEvents()

    # 不拿字节数当“截得对”的判据：设置页是白底 + 文字，PNG 压得很好，
    # 一万多字节完全可能是满有内容的一张。真正该量的是“画面里有多少不是背景”。
    for p in shots:
        assert p.exists() and p.stat().st_size > 4000, p
    bytes01 = shots[0].read_bytes()
    assert bytes01 != shots[1].read_bytes(), "未装与就绪两张一模一样，等于少看一张"
    assert shots[1].read_bytes() != shots[2].read_bytes()
