"""对战测试的共用夹具：真后端、真 QWebSocket，以及把一局推到终局的摆法。

为什么单独一个模块而不是各写一份：`set_up_endgame` 是整套桌面测试里最微妙的一处
（要让 AI 自己虚手，又不能绕过协议），两份拷贝迟早会漂移成两种行为。
"""
from __future__ import annotations

import uuid

from PySide6.QtCore import QObject

from core import api as A
from core import ws as W

PASSWORD = "mimashou123"
#: 终局形状用 9 路：只余 9 个单官点就能把 AI 逼到无路可下（19 路要填 19 个）。
ENDGAME_SIZE = 9
#: 与 backend/app/game/rules.py 同值的三常数。写成字面量而不是 import 服务端：
#: 本模块只有 `set_up_endgame` 一处必须碰到服务端代码（它要在内存里改棋盘），
#: 把 import 泄漏到每一行判断里，测试与后端的耦合面会不必要的宽。
EMPTY_, BLACK_, WHITE_ = 0, 1, 2


# ------------------------------------------------------------------ 事件录制

class Recorder(QObject):
    """把 GameSocket 的两个信号录成列表。

    槽必须是这个活得够久的 QObject 的**绑定方法**：连到 lambda 上会被静默丢投递
    （实测口径见 `core/api.py` 的 Reply 文档），症状是"测试超时但没有任何错误"。
    """

    def __init__(self):
        super().__init__()
        self.events: list[dict] = []
        self.statuses: list[str] = []

    def on_event(self, ev) -> None:
        self.events.append(ev)

    def on_status(self, status) -> None:
        self.statuses.append(status)

    def clear(self) -> None:
        self.events.clear()

    @property
    def types(self) -> list[str]:
        return [str(e.get("type")) for e in self.events]

    def first(self, kind: str) -> dict | None:
        for e in self.events:
            if e.get("type") == kind:
                return e
        return None

    def count(self, kind: str) -> int:
        return sum(1 for e in self.events if e.get("type") == kind)

    @property
    def state(self) -> dict:
        """最后一帧全量 state（连接首帧、悔棋、resume 都带它）。"""
        for e in reversed(self.events):
            if e.get("state"):
                return e["state"]
        return {}


# ------------------------------------------------------------------ REST 夹具

def register(host, tag: str = "dt") -> dict:
    """一个新账号。每个用例各开一个：新开对局要求"该用户没有进行中的局"，
    共用账号会互相挡出 409，那种失败看起来像是被测代码错了。"""
    name = tag + uuid.uuid4().hex[:8]
    data = A.http_json("POST", f"{host.base_url}/api/auth/register",
                       {"username": name, "password": PASSWORD, "displayName": "对战验收"})
    assert data and data.get("token"), f"注册没有返回 token：{data}"
    return {"name": name, "token": data["token"], "user": data["user"]}


def create_game(host, token: str, **body) -> dict:
    payload = {"size": ENDGAME_SIZE, "playerColor": 1, "komi": 7.5,
               "scoreMethod": "area", **body}
    data = A.http_json("POST", f"{host.base_url}/api/games", payload, token)
    assert data and data.get("game"), f"创建对局失败：{data}"
    return data


def fetch_game(host, token: str, game_id: str) -> dict:
    data = A.http_json("GET", f"{host.base_url}/api/games/{game_id}", None, token) or {}
    return data.get("game") or {}


def connect(host, token: str, game_id: str):
    """开一条对局连接。返回 (socket, recorder)，首帧由调用方等。"""
    rec = Recorder()
    sk = W.GameSocket(lambda: W.ws_url(host.base_url, f"/ws/game/{game_id}", token))
    sk.event.connect(rec.on_event)
    sk.statusChanged.connect(rec.on_status)
    sk.connect_to_game()
    return sk, rec


# ------------------------------------------------------------------ 终局形状

def set_up_endgame(game_id: str, size: int = ENDGAME_SIZE) -> dict:
    """把一局摆成「只剩中央一列单官」的终局形状，返回该形状的描述。

    **为什么必须在服务端摆**：AI 只在无路可下时才会虚手 —— `manager.should_pass`
    要求引擎首推 pass 且空点 <= 2×路数，而启发式引擎从不推 pass，所以真下满一盘
    9 路要 80 多手（每手还有 0.4 秒模拟思考），拿它当一个测试不划算。
    后端自己测结算用的就是同一手法（`backend/tests/test_api.py` 的终局用例）。

    摆出来的形状是两条**活棋**：左右两块实墙各自留了两个真眼，谁都吃不掉谁，
    所以之后填单官不会突然引发大转换；中间那一列是双方都能下的单官。

    刻意**不绕过协议**：这里只改变"局面长什么样"，之后的每一手仍然是
    客户端发 move/pass → 服务端判 AI 无路可走 → 双虚手 → `enter_scoring`
    → 客户端收到真实的 `scoring` 事件。
    """
    from app.game.manager import get_hub
    from app.game.rules import BLACK, EMPTY, WHITE

    live = get_hub().get(game_id)
    assert live is not None, "对局不在内存里，摆不了终局形状"
    g = live.game
    mid = size // 2
    for y in range(size):
        for x in range(size):
            g.board.grid[y][x] = BLACK if x < mid else WHITE
    dame = [(mid, y) for y in range(size)]
    # 两块墙各留两个眼（在各自内部），谁提不掉谁
    eyes = [(1, 1), (1, size - 3), (size - 2, 2), (size - 2, size - 4)]
    for x, y in dame + eyes:
        g.board.grid[y][x] = EMPTY
    g.board.pass_count = 0
    g.next_color = live.player_color
    g.finished = False
    return {"dame": dame, "eyes": eyes, "mid": mid}


def play_out_to_scoring(host, token: str, game_id: str, sk, rec, qapp,
                        wait, size: int = ENDGAME_SIZE, rounds: int = 24) -> tuple[dict, list]:
    """客户端把单官填完直到收到 `scoring`。全程真协议往返，返回 `(那条 scoring 事件, 本地棋盘)`。

    一并交出棋盘是因为调用方需要挑「盘上真有的子」，而不能拿引擎给的那份当依据：
    `set_up_endgame` 只改服务端 grid、不改 `moves`，而 `build_query` 是按
    `gtp_moves()` 重放的 —— 引擎看到的局面与盘上形状不是一回事（详见
    `test_ws_flow.py` 里那段注释）。两个引擎在这件事上给出的答案不同，
    拿它们任一家的输出当前提都会把测试绑到引擎行为上。

    `wait` 是 `harness.wait`：必须由调用方给，因为这个循环要跑 Qt 事件循环，
    而 pytest-qt 之类的新依赖不在这个项目的账上。

    **为什么自己维护一份本地棋盘**：`GET /api/games/{id}` 的 payload 里没有 `board`
    （只有手顺与分析），拿 `payload["board"]` 筛空点会永远筛出一个空列表，于是
    “填单官”变成“一路 pass” —— 码不出错、测试也能过，但测的就不是落子了。
    本地这一份从 `set_up_endgame` 摆好的形状起，按收到的 move/aiMove 增量更新。
    """
    shape = set_up_endgame(game_id, size)
    mid = shape["mid"]
    grid = [[BLACK_ if x < mid else WHITE_ for x in range(size)] for _ in range(size)]
    for x, y in shape["dame"] + shape["eyes"]:
        grid[y][x] = 0
    for _ in range(rounds):
        empties = [(x, y) for x, y in shape["dame"] if grid[y][x] == 0]
        rec.clear()          # 先清再发：下面按"这一轮到了什么"判定，不能带着上一轮的旧帧
        if empties:
            x, y = empties[0]
            sk.send({"action": "move", "x": x, "y": y})
        else:
            sk.send({"action": "pass"})
        done = wait(qapp, lambda: rec.first("scoring") is not None
                    or rec.first("gameEnd") is not None
                    or rec.first("error") is not None
                    or rec.first("aiMove") is not None, timeout=30.0)
        if not done:
            raise AssertionError(f"填单官没有推进：已收到 {rec.types}")
        if rec.first("error"):
            raise AssertionError(f"填单官被服务端拒绝：{rec.first('error')}")
        for ev in rec.events:
            if ev.get("type") not in ("move", "aiMove"):
                continue
            m = ev.get("move") or {}
            mx, my = m.get("x"), m.get("y")
            if mx is not None and my is not None:
                grid[my][mx] = m.get("color")
                for cx, cy in (m.get("captures") or []):
                    grid[cy][cx] = 0
        ev = rec.first("scoring")
        if ev:
            return ev, grid
    raise AssertionError("填完单官仍没进入终局结算（AI 没有按预期虚手）")
