"""P5 验收：把正在跑的 katago.exe **真杀掉**，看界面会不会说人话。

只有带 `GO_KATAGO_ENABLED=true` 的那一支才跑（见 `harness.katago_requested`）：
这一支要等 GPU 重载 90MB 网络，分钟级，塞进默认那一支会让每次回归都变贵。

为什么值得真杀而不是摆桩：设置页四态的措辞已经用桩钉过（`test_settings_flow.py`），
但桩只钉得住「我以为后端会回什么」；真断链时后端**实际**回什么是另一回事。
本轮就是靠这一次真杀抓到两处：
  · `recover.attempt` 在断链后的头 5 秒真的是 0（看门狗先睡再置 1），
    大厅照原样印就成「第 0 次重试」—— 网页版早就写了 `attempt || 1`，桌面漏了；
  · 自愈完成后 `deathCount` 不归零。桩数据里那条已经钉过（不能报成正在重启），
    这里是拿真链路再验一次：真值 + 真 GET + 真绘制，中间不经过我的手写 dict。
顺带把 `test_settings_flow.py` 第 9 行那句「KataGo 真断链那一条在这里」变成真话
—— 那句引用写出来时这个文件并不存在（假验收，见项目日志第 18 轮）。
"""
from __future__ import annotations

import subprocess
import time
import uuid

import pytest

from core import api as A
from core import backend_host as bh
from ui.pages import lobby
from ui.pages import settings as st
from tests import harness as H
from tests import test_settings_flow as TSF

pytestmark = pytest.mark.skipif(
    not H.katago_requested(),
    reason="这一支要真杀引擎进程并等它自愈（分钟级），只在要求 KataGo 就绪时跑")

PASSWORD = "mimashou123"

#: 两步之间的现场。第二步依赖第一步已经把引擎杀掉，所以它开头有 `ctx` 非空断言
#: （单跑 `-k healed` 会是红的，而不是"看起来绿了但其实什么都没测"）。
ctx: dict = {}


def _pool():
    """引擎池单例。**必须延迟 import**：`app.config` 在 import 时就读 `GO_DATA_DIR`，
    而 collect 阶段夹具还没把数据目录指到 tmp —— 早一步 import 就把这一支的库与日志
    写进了真实的 `backend/data`（`backend_host._start_once` 里那句 `paths.apply_env()`
    才是顺序的保证，所以只能在 host 起好之后拿）。"""
    from app.engine import pool as ep
    return ep.pool


@pytest.fixture(scope="module")
def host():
    h = bh.BackendHost()
    h.start(timeout=90.0)
    yield h
    h.stop()


@pytest.fixture(scope="module")
def account(host):
    """`/api/system/status` 要鉴权（`Depends(get_current_user)`），所以得有个真账号。"""
    username = "kt" + uuid.uuid4().hex[:8]
    data = A.http_json("POST", f"{host.base_url}/api/auth/register",
                       {"username": username, "password": PASSWORD,
                        "displayName": "断链验收"})
    assert data and data.get("token"), f"注册没有返回 token：{data}"
    return {"username": username, "token": data["token"]}


@pytest.fixture(scope="module")
def status_url(host):
    return A.build_url(host.base_url, "/api/system/status")


@pytest.fixture(scope="module")
def live(host, account, status_url):
    """等到引擎真的就位，并把「就位时的断链历史」记下来当基线。

    不等就往下走，`active` 会合法地是 `heuristic`（预热是后台任务），那一步的
    所有断言都会红在一个根本没坏的地方 —— 理由同 `harness.wait_engine_active`。
    """
    eng, err = H.wait_engine_active(status_url, account["token"], "katago", timeout=600.0)
    kat = eng.get("katago") or {}
    assert eng.get("active") == "katago", (
        f"这一支要求引擎真就绪：warming={eng.get('warming')} "
        f"available={kat.get('available')} error={kat.get('error')!r} "
        f"等待期异常={err!r} stderr={kat.get('stderrLog')}")
    ctx["deaths_before"] = int(kat.get("deathCount") or 0)
    return eng


def _image_name(pid: int) -> str:
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                       capture_output=True, encoding="utf-8", errors="replace")
    return ((r.stdout or "") + (r.stderr or "")).strip()


def _kill(pid: int) -> None:
    r = subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, (
        f"taskkill /F /PID {pid} 没成功（{(r.stdout or '') + (r.stderr or '')}）。"
        f"杀不掉引擎这一支就没有现场可验，红在这里比假装绿着有用")


def _wait_dead(status_url: str, token: str, timeout: float = 30.0) -> dict:
    """轮询到「引擎已经不在了、而看门狗还在睡」那一瞬，返回整包 `/api/system/status`。

    判据为什么是这三条而不是只看 `active`：`available=False` 之后马上就会进入
    `start()`（`warming=True`），设置页那一态随即变成「预热中」。5 秒宽的
    `recovering` 窗口是唯一能验「断链自愈中」这句措辞的现场，必须等它而不是等完。
    """
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = A.http_json("GET", status_url, token=token, timeout=10.0) or {}
        eng = last.get("engine") or {}
        kat = eng.get("katago") or {}
        if (eng.get("active") != "katago" and not eng.get("warming")
                and int(kat.get("deathCount") or 0) > ctx["deaths_before"]):
            return last
        time.sleep(0.2)
    raise AssertionError(
        f"{timeout:.0f} 秒内没看到断链现场（PID {ctx.get('pid')} 已杀），"
        f"最后一次回包：{last}")


# ---------------------------------------------------------------- 现场：引擎死了

def test_killing_the_engine_makes_the_badge_say_recovering(qapp, host, account,
                                                           status_url, live):
    """杀掉进程 → 界面必须说「断链自愈中」，而且说的每一个数字都得是真的。"""
    kat = _pool().katago
    assert kat.proc is not None and kat.proc.returncode is None, "抓不到活着的引擎进程"
    pid = int(kat.proc.pid)
    # 先确认这个 PID 真是 katago.exe：拿别人的进程去杀，测试会以另一种方式成功
    # （引擎还活着 → 后面等不到断链现场），红得完全看不出原因。
    assert "katago" in _image_name(pid).lower(), f"PID {pid} 不是引擎进程：{_image_name(pid)}"
    ctx["pid"] = pid

    _kill(pid)
    dead = _wait_dead(status_url, account["token"])
    ctx["dead_raw"] = dead
    eng = dead["engine"]
    k = eng["katago"]
    ctx["engine"] = eng
    ctx["deaths_at_death"] = int(k["deathCount"])

    assert k["available"] is False and eng["active"] == "heuristic"
    assert int(k["deathCount"]) == ctx["deaths_before"] + 1, (
        f"断链计数应当正好加一（多出来的那一杀不是本测试干的）："
        f"前={ctx['deaths_before']} 现在={k['deathCount']}")
    # 计划里那句「recover.watching 变 True」的真值在这里：它本来就恒 True（看门狗
    # 任务还在），能区分的是「还在试」与「已放弃」—— 所以断言要连着 deathCount 一起看
    assert eng["recover"]["watching"] is True
    assert (k["error"] or k["lastDeathReason"]), f"死因没落到状态里：{k}"
    assert k["lastDeathAt"], "断链时间没记，界面那句「最近一次」就会空着"
    # 真杀一次才看到的：旧实现的 EOF 死因去拼 stderr 尾部，而运行期的 stderr 里
    # 只有开机那几行 —— 界面就会把「Loaded config | Loaded model | Started, ready to
    # begin handling requests」当成死因贴出来（那不是死因，那是一句“我很好”）。
    reason = k["error"] or k["lastDeathReason"]
    assert "Loaded config" not in reason and "ready to begin" not in reason, reason
    assert len(reason) < 200, f"一行状态里装了 {len(reason)} 字日志：{reason}"

    # ---------------- 界面措辞：吃的是上面那份**真回包**
    assert st.engine_state(eng) == "recovering"
    _kind, line = lobby.engine_state(dead)
    assert "第 0 次" not in line, f"大厅把第一次重试报成了第 0 次：{line}"
    assert "第 1 次" in line, line
    assert f"最多 {eng['recover']['maxAttempts']} 次" in line, line

    w = TSF.build(qapp)
    try:
        w._status = dead                        # 整包真回包，不掺桩
        w._paint_engine()
        qapp.processEvents()
        assert w.engineBadge.text() == "断链自愈中", w.engineBadge.text()
        detail = w.engineDetail.text()
        assert f"断链 {k['deathCount']} 次" in detail, detail
        assert "正在自动重启" in w.engineHint.text(), w.engineHint.text()
        assert "无需重装" in w.engineHint.text(), w.engineHint.text()
        assert "未安装" not in w.engineBadge.text() + detail, "断链被说成了没装"
        assert w.btnInstall.isVisibleTo(w) is False, "自愈中给一个再下 90MB 的按钮"
        offenders, scanned = H.clipped_texts(w)
        # 下限取本项实测值（真回包下扫到 34 个）而不是猜一个：这一条要防的是
        # “页面根本没画出来就宣布没裁字”，而不是给控件个数当规格
        assert scanned >= 30, f"只扫到 {scanned} 个控件，这一条根本没在看页面"
        assert not offenders, "真路径比桩路径长，裁字只在真数据下现形：" + "；".join(offenders)
        # 看图钉的两条（`p5a_01_recovering.png`）：
        #   ① 断链那一瞬「当前引擎」仍然有人话可读 —— 清成「—」会被读成
        #      「引擎被卸了」，而用户接下来就会去点那个本轮明确藏起来的安装按钮；
        #   ② 引擎路径/权重路径是部署细节，第 33 轮起不再摆上屏（清除开发者痕迹），
        #      断链现场该看的信息全在 engineBadge / engineDetail / engineHint 里。
        assert w.kv["当前引擎"].fullText() not in ("", "—"), w.kv["当前引擎"].fullText()
        ctx["frame"] = H.snap(w, "p5a_01_recovering")
    finally:
        w.shutdown()
        w.close()


# ---------------------------------------------------------------- 现场：它自己好了

def test_it_heals_and_the_badge_stops_shouting(qapp, host, account, status_url):
    """自愈完成后必须改口回「就绪」。这一条管的是长期挂着「正在自动重启」的假警报。

    `deathCount` 是历史计数，重启成功不会归零 —— 只看它的实现会在一台完全正常的
    机器上永久报故障（网页版就说过这句「正在自动重启」永不消失，桩那一侧也钉过）。
    这里验的是：真链路取回的真数据仍然落到 ready。
    """
    assert ctx.get("engine"), "上一拍没跑（单独 -k 这一条会红，这是应该的）"

    eng, err = H.wait_engine_active(status_url, account["token"], "katago", timeout=600.0)
    assert eng.get("active") == "katago", (
        f"看门狗没把引擎拉回来：warming={eng.get('warming')} "
        f"recover={eng.get('recover')} error={(eng.get('katago') or {}).get('error')!r} "
        f"等待期异常={err!r} stderr={(eng.get('katago') or {}).get('stderrLog')}")
    k = eng["katago"]
    assert int(k["deathCount"]) == ctx["deaths_before"] + 1, "自愈不该又新增一次断链"
    assert k["available"] is True and k["warming"] is False and k["error"] == ""
    assert eng["recover"]["attempt"] == 0, (
        "重启预算没归零：下一次断链就只剩更少的重试次数（见 pool._supervise）")

    _kind, line = lobby.engine_state({"engine": eng})
    assert _kind == "ready" and line.startswith("KataGo 就绪"), line
    assert st.engine_state(eng) == "ready"
    head, tail = st.engine_detail(eng)
    assert "自愈" in tail and "无需重装" in tail, tail
    assert "正在自动重启" not in head + tail, f"已经好了还在报故障：{head} / {tail}"

    # ---------------- 真客户端链路：页面自己去 GET，不喂任何手写数据
    # 两张关键帧的「API Key」一栏不一样（01 有「已配置」小标签、02 没有）不是缺陷：
    # 01 用的是 `TSF.build()` 那个桩账号，02 用的是本支刚注册的新账号 ——
    # 它本来就没配 Key。看图时先问「这两个账号是同一个吗」再下结论。
    api = A.ApiClient(lambda: host.base_url, lambda: account["token"])
    w = st.SettingsPage(api, TSF.FakeSound(), TSF.FakePrefs())
    w.resize(1104, 760)
    w.show()
    qapp.processEvents()
    try:
        w.refresh()
        assert H.wait(qapp, lambda: w._status and w._user), "两个真 GET 没都回来"
        assert w.engineBadge.text() == "KataGo 就绪", w.engineBadge.text()
        assert f"自愈 {ctx['deaths_at_death']} 次" in w.engineHint.text(), w.engineHint.text()
        assert w.kv["当前引擎"].text() == "KataGo", w.kv["当前引擎"].text()
        assert H.blank_ratio(w) > 0.4, "整页几乎全白"
        ctx["frame_ready"] = H.snap(w, "p5a_02_healed")
    finally:
        w.shutdown()
        w.close()

    # 引擎进程真的换了一个：证明「回来了」不是状态位被谁手动抹平
    assert _pool().katago.proc is not None and int(_pool().katago.proc.pid) != ctx["pid"], (
        f"PID 没变（{ctx['pid']}），说明自愈后跑的还是原来那个进程")
