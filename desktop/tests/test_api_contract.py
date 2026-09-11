"""桌面端 REST 层 ↔ 真后端的契约验收。

跑的是**真后端**（内嵌在同进程里那个），不是 mock：网页版已经冻结，它是
桌面端与后端之间唯一的"共同语言文档"，能钉住契约的只剩这里。用 mock 只会
把我对字段的猜测固化成测试 —— 那种测试全绿而界面白板。

顺带钉住两件事：
  · `GO_DATA_DIR` 这条路径缝真的通到后端（数据库落在临时目录，不碰 backend/data）；
  · 错误文案与 `frontend/src/api/client.ts` 同口径（用户看到的句子不该换语言）。
"""
from __future__ import annotations

import gc
import threading
import time
import uuid

import pytest

from PySide6.QtCore import QObject

from core import api as A
from core import backend_host as bh
from tests import harness as H

PASSWORD = "mimashou123"


@pytest.fixture(scope="module")
def host():
    h = bh.BackendHost()
    h.start(timeout=90.0)
    yield h
    h.stop()


@pytest.fixture(scope="module")
def client(host, account):
    """带着已登录 token 的客户端：只读接口全部要鉴权。"""
    return A.ApiClient(lambda: host.base_url, lambda: account["token"])


@pytest.fixture(scope="module")
def account(host):
    """注册一个当轮专用的新用户：桌面验收不该去动已有账号的数据。"""
    username = "dt" + uuid.uuid4().hex[:8]
    data = A.http_json("POST", f"{host.base_url}/api/auth/register",
                       {"username": username, "password": PASSWORD,
                        "displayName": "桌面验收"})
    assert data and data.get("token"), f"注册没有返回 token：{data}"
    return {"username": username, "token": data["token"], "user": data["user"]}


def _url(host, path, query=None):
    return A.build_url(host.base_url, path, query)


def _drain(qapp, predicate, timeout: float = 10.0) -> bool:
    """转主线程事件直到 predicate 为真。

    为什么不能写成 `for _ in range(60): qapp.processEvents()`：那样只能处理
    **已经投递到队列里**的事件，而工作线程的 emit 还在路上 —— 紧循环把 60 轮
    在几毫秒内跑完，看上去就是“信号没触发”。真实客户端没这个问题（主循环
    本来就在转），但测试里必须自己给它时间。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------- 认证

def test_register_returns_token_and_user_shape(host, account):
    user = account["user"]
    for key in ("id", "username", "displayName", "createdAt", "hintMode", "demotionEnabled"):
        assert key in user, f"登录响应缺字段 {key}：{sorted(user)}"
    assert user["username"] == account["username"]
    assert user["displayName"] == "桌面验收"
    # llmConfig 只回传"有没有配 Key"，绝不回传明文 —— 桌面端设置页要照这个口径显示
    assert set(user["llmConfig"]) == {"baseUrl", "model", "hasApiKey"}
    assert user["llmConfig"]["hasApiKey"] is False


def test_progress_has_every_field_the_shell_shows(account):
    """生涯统计与晋升进度：shell/lobby 要显示的每个键都得真在。"""
    p = account["user"]["progress"]
    required = {
        "rankId", "rankName", "short", "aiName", "aiTitle", "elo",
        "rankWins", "winsRequired", "rankLosses", "winStreak", "losingStreak",
        "inPromotion", "promotionWins", "promotionRequired",
        "maxAvgLossPoints", "promoAvgLossPoints",
        "totalGames", "totalWins", "avgLossPoints", "isMaxRank", "hint",
    }
    missing = required - set(p)
    assert not missing, f"progress 缺键：{sorted(missing)}"
    assert p["totalGames"] == 0 and p["totalWins"] == 0     # 新账号
    assert isinstance(p["rankId"], int) and isinstance(p["hint"], str)


def test_login_with_wrong_password_keeps_server_message(host, account):
    with pytest.raises(A.ApiError) as exc:
        A.http_json("POST", _url(host, "/api/auth/login"),
                    {"username": account["username"], "password": "cuowu12345"})
    assert exc.value.status == 401
    assert exc.value.args[0] == "用户名或密码错误"


def test_validation_error_is_flattened_into_one_line(host):
    """422 的 `detail` 是 list[dict]，直接 str() 会给用户看 Python 字面量。"""
    with pytest.raises(A.ApiError) as exc:
        A.http_json("POST", _url(host, "/api/auth/register"),
                    {"username": "a", "password": "123"})
    assert exc.value.status == 422
    msg = exc.value.args[0]
    assert "username" in msg and "password" in msg, msg
    assert "[" not in msg and "{" not in msg, f"没有被摊平：{msg}"


def test_me_requires_a_token(host):
    with pytest.raises(A.ApiError) as exc:
        A.http_json("GET", _url(host, "/api/auth/me"))
    assert exc.value.status in (401, 403)


def test_me_with_token_returns_the_same_user(host, account):
    data = A.http_json("GET", _url(host, "/api/auth/me"), token=account["token"])
    assert data["user"]["username"] == account["username"]


# ---------------------------------------------------------------- 只读页面数据

def test_system_status_engine_four_states(host, account):
    """引擎徽章要能区分「就绪 / 预热中 / 断链自愈中 / 没装」，靠的就是这几个键。"""
    wait_err = ""
    if H.katago_requested():
        # 预热是后台任务（`pool.startup()` 丢下任务就返回），**host 就绪 ≠ 引擎就绪**：
        # 不先等到切换完，下面读到的 `active` 会是合法的 heuristic，这一支就白跑了。
        _, wait_err = H.wait_engine_active(_url(host, "/api/system/status"),
                                           account["token"])
    data = A.http_json("GET", _url(host, "/api/system/status"), token=account["token"])
    eng = data["engine"]
    assert set(eng) >= {"active", "warming", "katago", "fallback", "recover"}
    # 坑：键叫 `fallback`，值却是 `heuristic`（内置启发式引擎的名字）。
    # 界面拿 active 做文案时必须用值，不是用键。
    assert eng["active"] in ("katago", "heuristic")
    k = eng["katago"]
    assert set(k) >= {"available", "warming", "binary", "model", "humanModel",
                      "error", "stderrLog", "deathCount", "lastDeathAt", "lastDeathReason"}
    assert set(eng["recover"]) >= {"watching", "attempt", "maxAttempts"}
    # 引擎这一段分两支跑（计划对 P2 的口径：KataGo 就绪 / 未就绪两种都要跑），
    # 所以这里**不能**写死启发式，只能看开关：写死了另一支会在第一条断言上就红，
    # 红得看不出是「引擎不对」还是「测试不对」（见 harness.katago_requested）。
    if H.katago_requested():
        # 起不来就是环境没装好，把死因与 stderr 路径一起带出来 ——
        # 只报一句“不是 katago”等于让人再去手动敲一遍命令行。
        assert eng["active"] == "katago", (
            f"这一支要求 KataGo 真就绪（已等过一轮预热）："
            f"warming={eng.get('warming')} available={k['available']} "
            f"error={k['error']!r} 等待期异常={wait_err!r} stderr={k['stderrLog']}")
        assert k["available"] is True and k["warming"] is False
    else:
        assert eng["active"] == "heuristic"
    assert k["deathCount"] == 0
    for block in ("resign", "review", "demotion"):
        assert block in data, f"缺 {block} 配置块"
    assert set(data["resign"]) == {"consecutiveMoves", "scoreThreshold", "winrateThreshold"}
    assert set(data["review"]) == {"visits", "thresholds"}
    assert set(data["review"]["thresholds"]) == {"slow", "bad", "blunder"}


def test_games_list_and_active_shape(host, account):
    data = A.http_json("GET", _url(host, "/api/games", {"limit": 30}),
                       token=account["token"])
    assert isinstance(data["items"], list) and data["items"] == []   # 新账号没有对局
    assert data["total"] == 0
    active = A.http_json("GET", _url(host, "/api/games/active"), token=account["token"])
    assert active["game"] is None
    timeline = A.http_json("GET", _url(host, "/api/auth/me/timeline"), token=account["token"])
    assert isinstance(timeline["items"], list)


def test_ranks_table_is_complete(host, account):
    data = A.http_json("GET", _url(host, "/api/ranks"), token=account["token"])
    assert len(data["items"]) == 27, f"段位表应当是 27 档：{len(data['items'])}"
    assert data["currentRankId"] is not None
    first = data["items"][0]
    assert set(first["engine"]) == {"maxVisits", "tolerance", "topN", "blunderRate",
                                    "localNoise", "ponder", "humanModel"}
    # tolerance 的单位是「目」，段位表页直接把它写进文案，所以必须是数字
    assert isinstance(first["engine"]["tolerance"], (int, float))


# ---------------------------------------------------------------- 路径缝

def test_data_dir_seam_reaches_the_backend(host, isolated_env):
    """`GO_DATA_DIR` 必须真的把 SQLite 也带走 —— 这是打包的前置条件。"""
    from app.config import settings          # 由 paths.apply_env() 放进 sys.path

    assert (isolated_env / "go_teach.db").exists(), \
        f"数据库没落在隔离目录：{settings.database_url}"
    assert isolated_env.as_posix() in settings.database_url


# ---------------------------------------------------------------- 异步层

class _Collector(QObject):
    """收信号的专用对象。

    为什么不用 lambda：实测 PySide6 6.11.2 上，同一个 Reply 的 `finished` 连到
    QObject 绑定方法时 8/8 送达，连到 lambda 时 0/8（静默丢投递）。
    页面代码必须遵守同一个约束 —— 这条在 `core/api.py` 的 Reply 文档里也写了。
    """

    def __init__(self):
        super().__init__()
        self.got: list[tuple] = []
        self.threads: list[int] = []

    def on_finished(self, data, err):
        self.got.append((data, err))
        self.threads.append(threading.get_ident())


def test_reply_delivers_on_the_main_thread(qapp, client, account):
    """跨线程 emit 必须排队回主线程，否则界面代码碰 Qt 对象就是未定义行为。"""
    col = _Collector()
    reply = client.get("/api/auth/me")
    reply.finished.connect(col.on_finished)
    data, err = reply.wait()
    assert err is None, err
    assert data["user"]["username"] == account["username"]
    assert col.threads == [threading.get_ident()], "信号在工作线程里触发了"
    assert col.got[0][0]["user"]["username"] == account["username"]


def test_reply_survives_the_caller_dropping_it(qapp, client, account):
    """`client.get(...)` 之后不留引用也不该丢投递 —— 客户端要替在途请求保活。

    这条是给 ApiClient._track 定的锚：Qt 对象归创建线程所有，工作线程回话时
    对象若已被 GC，症状是偶发的、栈不完整的崩溃。
    """
    col = _Collector()
    for _ in range(8):
        r = client.get("/api/health")
        r.finished.connect(col.on_finished)
        del r                                        # 故意不留引用
        gc.collect()
    assert client._pool.waitForDone(8000)
    assert _drain(qapp, lambda: len(col.got) == 8), f"8 个请求只回来 {len(col.got)} 个"
    assert all(err is None for _d, err in col.got), col.got
    assert set(col.threads) == {threading.get_ident()}, "有投递没回主线程"
    assert _drain(qapp, lambda: not client._inflight), "在途表没有随完成清空"


def test_connection_failure_reports_no_status(qapp, monkeypatch):
    """连不上时 status=0，文案要说"无法连接本地服务"，不能是 Python 异常repr。

    注意：这台机器的环境变量里挂着 `HTTP_PROXY`（由组织统一注入），而客户端走的是
    stdlib `urllib` —— 它会老实地把请求交给代理，于是「端口 1 没人听」变成「代理回了
    502」，`err.status` 是 502 而不是 0，本断言必红。那**不是**产品代码的问题：有代理
    时「服务不可达」本来就该表现为一个 HTTP 错误。本用例要验的是**传输层失败**这条
    分支的呈现，所以先把代理摘掉；`_opener` 也要置空，因为 `urlopen` 用的是缓存过的
    那一个（monkeypatch 会在用例结束后把两者都还原）。
    """
    import urllib.request

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(urllib.request, "_opener", None, raising=False)

    dead = A.ApiClient(lambda: "http://127.0.0.1:1")
    reply = dead.get("/api/health")
    data, err = reply.wait(timeout_ms=8000)
    assert data is None and isinstance(err, A.ApiError)
    assert err.status == 0
    assert "无法连接本地服务" in err.args[0]


def test_result_or_raise_turns_error_into_exception(client):
    reply = client.get("/api/health")
    assert reply.wait()[1] is None
    assert reply.result_or_raise()["ok"] is True

    dead = A.ApiClient(lambda: "http://127.0.0.1:1")
    bad = dead.get("/api/health")
    bad.wait(timeout_ms=8000)
    with pytest.raises(A.ApiError):
        bad.result_or_raise()


# ---------------------------------------------------------------- URL 拼装

def test_build_url_drops_none_and_encodes_lists():
    base = "http://127.0.0.1:9999"
    assert A.build_url(base, "/api/games") == base + "/api/games"
    assert A.build_url(base, "/api/games", {"limit": 30, "status": None}) == \
        base + "/api/games?limit=30"
    assert A.build_url(base, "/api/x?a=1", {"b": 2}) == base + "/api/x?a=1&b=2"
    got = A.build_url(base, "/api/games", {"sizes": [9, 13]})
    assert "sizes=9&sizes=13" in got, got
    assert A.build_url(base + "/", "/api/health") == base + "/api/health"
