"""端到端 API 测试：注册登录 → 开局 → WebSocket 对局 → 认输 → 复盘报告 → 导出。

这些用例跑在真实的 FastAPI 应用上（含 lifespan：引擎池 + 复盘 worker），
KataGo 与 LLM 均被禁用，验证的是「无外部依赖时平台全流程可用」这一承诺。
"""
from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.crypto import unseal
from app.engine.pool import MAX_RESTARTS
from app.game.manager import draw_color
from app.game.rules import BLACK, WHITE
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth(client):
    username = f"tester{int(time.time() * 1000) % 100000}"
    r = client.post("/api/auth/register", json={
        "username": username, "password": "secret123", "displayName": "小学棋"})
    assert r.status_code == 200, r.text
    data = r.json()
    headers = {"Authorization": f"Bearer {data['token']}"}
    return data["token"], headers, data["user"]


def test_health_and_ranks(client):
    assert client.get("/api/health").json()["ok"] is True
    ranks = client.get("/api/ranks").json()
    assert len(ranks["items"]) == 27
    assert ranks["items"][0]["name"] == "18级"
    assert ranks["items"][-1]["name"] == "九段"
    # 18级 必须真的弱。**不要改回断言 maxVisits 很小**：实测 visits=2 时 KataGo
    # 只报 1.1 个候选，AI 被迫下最优点，每手仅亏 0.82 目（职业量级）—— 反而最强。
    # visits 买的是候选池宽度与给学员的分析精度，削弱靠 localNoise / tolerance。
    first = ranks["items"][0]["engine"]
    assert first["localNoise"] >= 0.5 and first["tolerance"] >= 2.0
    assert first["maxVisits"] >= 32 and first["humanModel"] is True
    last = ranks["items"][-1]["engine"]
    assert last["localNoise"] == 0.0 and last["tolerance"] == 0.0


def test_register_login_me(client, auth):
    token, headers, user = auth
    assert user["progress"]["rankName"] == "18级"
    assert user["progress"]["winsRequired"] == 3
    assert user["llmConfig"]["hasApiKey"] is False

    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["user"]["username"] == user["username"]

    # 未带 token 应被拒绝
    assert client.get("/api/auth/me").status_code == 401
    # 密码错误
    bad = client.post("/api/auth/login", json={"username": user["username"],
                                               "password": "wrong-password"})
    assert bad.status_code == 401
    ok = client.post("/api/auth/login", json={"username": user["username"],
                                              "password": "secret123"})
    assert ok.status_code == 200 and ok.json()["token"]


def test_duplicate_username_rejected(client, auth):
    _, _, user = auth
    r = client.post("/api/auth/register", json={"username": user["username"],
                                                "password": "secret123"})
    assert r.status_code == 400


def test_system_status_reports_fallback_engine(client, auth):
    _, headers, _ = auth
    st = client.get("/api/system/status", headers=headers).json()
    assert st["engine"]["active"] == "heuristic"
    assert st["engine"]["katago"]["available"] is False
    assert st["llm"]["configured"] is False
    assert st["resign"]["scoreThreshold"] == settings.resign_score_threshold
    # 断链自愈的前端契约：少了这几个键，界面就会把「预热中/断链自愈中」报成「未装」
    assert st["engine"]["warming"] is False
    assert st["engine"]["katago"]["deathCount"] == 0
    assert st["engine"]["recover"] == {"watching": False, "attempt": 0,
                                       "maxAttempts": MAX_RESTARTS}


def test_create_game_and_state(client, auth):
    _, headers, _ = auth
    r = client.post("/api/games", json={"size": 9, "komi": 5.5, "playerColor": 1,
                                        "hintMode": True}, headers=headers)
    assert r.status_code == 200, r.text
    game = r.json()["game"]
    assert game["size"] == 9 and game["phase"] == "playing"
    assert game["rankName"] == "18级" and game["aiName"]
    assert game["allowTakeback"] is True
    assert game["nextColor"] == 1          # 玩家执黑先走

    # 同一用户不能同时开两局
    again = client.post("/api/games", json={"size": 9}, headers=headers)
    assert again.status_code == 409

    active = client.get("/api/games/active", headers=headers).json()
    assert active["game"]["id"] == game["id"]


def test_draw_color_yields_both_colors():
    """猜先必须真的两种颜色都会出现。

    只断言「返回值合法」是不够的——硬编码成 BLACK 也能过那条。跑 200 次，
    黑白都得出现（真随机时全落一侧的概率是 2^-199，不会偶发红）。
    """
    seen = {draw_color() for _ in range(200)}
    assert seen == {BLACK, WHITE}, f"抽取 200 次只出现 {sorted(seen)}，不像是随机的"


def test_create_game_with_color_draw(client, auth):
    """开局可以选「抽取」（playerColor=0），由服务端猜先决定执黑还是执白。

    抽取放在服务端而不是前端：前端抽的话用户可以反复抽到满意为止，那就不是猜先了。
    所以这里断言的是**契约**：回来的必定是 1 或 2（0 绝不能透给前端），
    且先后手要跟着抽取结果走。随机性本身由 test_draw_color_yields_both_colors 负责，
    这里**故意不断言「两种颜色都抽到过」**——那会让测试偶发红。
    """
    _, headers, _ = auth
    for _ in range(6):
        r = client.post("/api/games", json={"size": 9, "komi": 5.5, "playerColor": 0},
                        headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        game = body["game"]
        assert game["playerColor"] in (BLACK, WHITE), f"把抽取标记透出去了：{game['playerColor']}"
        assert game["colorSource"] == "guess", "抽取局的来源必须是 guess"
        if game["playerColor"] == BLACK:
            # 抽到黑：玩家先走，开局不该有任何一手
            assert game["nextColor"] == BLACK and game["moveCount"] == 0
            assert body["events"] == []
        else:
            # 抽到白：分先棋恒为黑先，所以 AI 必须先落一子。
            # moveCount 曾因 rec 未 refresh 而报 0（返回体带着 aiMove 事件却说 0 手）
            assert game["moveCount"] == 1, "抽到白时 AI 应先走，返回体却没带那一手"
            assert game["nextColor"] == WHITE, "AI 先走一轮到玩家（白）"
            assert game["moves"][0]["color"] == BLACK, "先走的那手必须是 AI 的黑棋"
            assert [e["type"] for e in body["events"]] == ["analysis", "aiMove"]
        assert client.delete(f"/api/games/{game['id']}", headers=headers).status_code == 200


def test_handicap_overrides_color_draw(client, auth):
    """让子棋优先于抽取：handicap>=2 时不论传什么都固定玩家执黑。

    前端在让子棋时禁用了执子下拉，但状态里可能还留着「抽取」，
    所以服务端必须自己兜住——manager.create 里先判让子、再判抽取，顺序不能反。
    """
    _, headers, _ = auth
    r = client.post("/api/games", json={"size": 9, "komi": 0.5, "handicap": 4,
                                        "playerColor": 0}, headers=headers)
    assert r.status_code == 200, r.text
    game = r.json()["game"]
    assert game["playerColor"] == BLACK, "让子棋必须玩家执黑，抽取不能盖掉这条规则"
    assert game["colorSource"] == "rule", "让子强制执黑不能说成抽到了黑"
    # 让子棋摆好让子后由 AI（白）先走
    assert game["moveCount"] == 1 and game["nextColor"] == BLACK
    assert game["moves"][0]["color"] == WHITE
    assert client.delete(f"/api/games/{game['id']}", headers=headers).status_code == 200


def test_color_source_recorded_in_create_and_list(client, auth):
    """L15：执子来源入库 —— 抽的还是选的、被让子规则强制的，详情与列表载荷都能区分。

    旧口径只有 playerColor（结果），猜先与自选分不开、统计也无从谈起；现在
    color_source 与结果一起落库，大厅「猜先」徽章与将来的统计都靠它。
    导入棋谱的来源在 test_import_sgf_creates_reviewable_game 里另验（import）。
    """
    _, headers, _ = auth
    # 抽取：guess；且列表载荷（record_brief）必须带同一个值
    r = client.post("/api/games", json={"size": 9, "komi": 5.5, "playerColor": 0},
                    headers=headers)
    assert r.status_code == 200, r.text
    gid = r.json()["game"]["id"]
    assert r.json()["game"]["colorSource"] == "guess"
    rows = client.get("/api/games", headers=headers).json()["items"]
    row = next(x for x in rows if x["id"] == gid)
    assert row["colorSource"] == "guess", "列表载荷丢了 colorSource"
    assert row["playerColor"] in (BLACK, WHITE)
    client.delete(f"/api/games/{gid}", headers=headers)

    # 显式指定执黑：pick
    r = client.post("/api/games", json={"size": 9, "komi": 5.5, "playerColor": 1},
                    headers=headers)
    game = r.json()["game"]
    assert game["colorSource"] == "pick", "显式选子必须记成 pick"
    client.delete(f"/api/games/{game['id']}", headers=headers)

    # 让子棋 + 抽取：被规则强制执黑，来源是 rule（不能算「抽到了黑」）
    r = client.post("/api/games", json={"size": 9, "komi": 0.5, "handicap": 4,
                                        "playerColor": 0}, headers=headers)
    assert r.status_code == 200, r.text
    game = r.json()["game"]
    assert game["colorSource"] == "rule" and game["playerColor"] == BLACK
    client.delete(f"/api/games/{game['id']}", headers=headers)


def test_player_color_rejects_out_of_range(client, auth):
    """playerColor 只接受 0（抽取）/1/2；越界要 422，不能被默默当成抽取。"""
    _, headers, _ = auth
    for bad in (3, -1):
        r = client.post("/api/games", json={"size": 9, "playerColor": bad}, headers=headers)
        assert r.status_code == 422, f"playerColor={bad} 应被拒绝，实际 {r.status_code}"


def test_websocket_full_move_cycle(client, auth):
    token, headers, _ = auth
    created = client.post("/api/games", json={"size": 9, "komi": 5.5, "hintMode": True},
                          headers=headers).json()["game"]
    assert created["colorSource"] == "pick", "显式指定执子必须是 pick"
    gid = created["id"]

    with client.websocket_connect(f"/ws/game/{gid}?token={token}") as ws:
        state = ws.receive_json()
        assert state["type"] == "state"
        assert state["state"]["phase"] == "playing"

        ws.send_json({"action": "move", "x": 2, "y": 6})
        types, ai_move, analysis = [], None, None
        for _ in range(6):
            msg = ws.receive_json()
            types.append(msg["type"])
            if msg["type"] == "aiMove":
                ai_move = msg["move"]
            if msg["type"] == "analysis":
                analysis = msg["analysis"]
            if msg["type"] == "aiMove":
                break
        assert "move" in types and "analysis" in types and "aiMove" in types
        assert ai_move is not None
        assert ai_move["color"] == 2                      # AI 执白
        assert 0 <= ai_move["x"] < 9 and 0 <= ai_move["y"] < 9
        assert analysis["winrateBlack"] is not None
        assert 0.0 <= analysis["winrateBlack"] <= 1.0
        assert analysis["candidates"], "分析应给出候选点（供提示与复盘用）"

        # 悔棋：两手一起撤销（后台分析事件可能先到达，读到 takeback 为止）
        ws.send_json({"action": "takeback", "plies": 2})
        tb = ws.receive_json()
        for _ in range(4):
            if tb["type"] == "takeback":
                break
            tb = ws.receive_json()
        assert tb["type"] == "takeback"
        assert tb["moveCount"] == 0
        assert len(tb["state"]["analyses"]) <= 1

        # 非法落点
        ws.send_json({"action": "move", "x": 99, "y": 0})
        err = ws.receive_json()
        assert err["type"] == "error"

        # 认输 → 终局事件带等级进度
        ws.send_json({"action": "resign"})
        end = ws.receive_json()
        while end["type"] != "gameEnd":
            end = ws.receive_json()
        assert end["playerWon"] is False
        assert end["winner"] == 2
        assert end["rank"]["progress"]["totalGames"] == 1
        assert end["rank"]["progress"]["rankName"] == "18级"   # 输了不升级
        assert end["sgf"].startswith("(;FF[4]")
        assert end["aiWords"]


def test_websocket_rejects_bad_token(client, auth):
    _, headers, _ = auth
    gid = client.post("/api/games", json={"size": 9}, headers=headers).json()["game"]["id"]
    with client.websocket_connect(f"/ws/game/{gid}?token=not-a-token") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"


def test_hint_query_uses_review_caliber_no_human_model(client, auth, monkeypatch):
    """对局提示（hint）必须与复盘同口径：主网络 + review_visits。

    现场问题：提示曾走档位 human 风格模型 + 档位 visits，玩家「完全按 KataGo
    推荐下」，复盘却按主网络首选评判——两把尺子，跟提示也被报大损失。

    第 39 轮审计后加固：原来这条用 FakePool 整体替换 `get_pool()`，`pool.analyze`
    里「profile 覆写查询」的逻辑根本没执行——提示 query 设好了 review_visits +
    主网络，档位 profile 一到就被写回 human 模型与档位 visits，§31 的修复在真实
    路径上从未生效（假绿）。现在走**真 EnginePool**，只替换 katago 查询层：
    断言取的是真 `pool.analyze` 之后的查询，覆写若复发当场现形。
    """
    from app.config import settings
    from app.engine.pool import get_pool
    from app.engine.protocol import AnalysisResult, Candidate

    captured: dict = {}

    class FakeKatago:
        available = True

        async def analyze(self, query, side_to_move, turn):
            captured["query"] = query
            return AnalysisResult(candidates=[
                Candidate(point=(2, 2), gtp="C3", visits=96, winrate=0.62, score_lead=2.0)])

    pool = get_pool()
    monkeypatch.setattr(pool, "katago", FakeKatago())

    token, headers, _ = auth
    gid = client.post("/api/games", json={"size": 9, "komi": 5.5, "hintMode": True},
                      headers=headers).json()["game"]["id"]
    with client.websocket_connect(f"/ws/game/{gid}?token={token}") as ws:
        state = ws.receive_json()
        assert state["type"] == "state"
        ws.send_json({"action": "hint"})
        got = None
        for _ in range(6):
            msg = ws.receive_json()
            if msg["type"] == "hintOnly":
                got = msg
                break
        assert got and got["hint"], got
    q = captured["query"]
    assert q.human_sl_profile == "", f"提示还在用人类风格模型：{q.human_sl_profile!r}"
    assert q.max_visits == settings.review_visits, \
        f"提示 visits {q.max_visits} 与复盘标尺 {settings.review_visits} 不一致"


def test_player_pass_gets_response(client, auth):
    """玩家虚手后必须立即得到回应（自己的那手先回显，AI 落子或进入结算随后）。

    这是「落子后要等 AI 思考完才显示」问题的验收点：move 事件必须第一个到达，
    不能憋在 AI 回合结束后的批量事件里。
    """
    token, headers, _ = auth
    gid = client.post("/api/games", json={"size": 9, "komi": 5.5},
                      headers=headers).json()["game"]["id"]
    with client.websocket_connect(f"/ws/game/{gid}?token={token}") as ws:
        ws.receive_json()                      # state
        ws.send_json({"action": "pass"})
        types = [ws.receive_json()["type"]]
        assert types[0] == "move", "玩家自己的手必须立即回显，不能等 AI 回合结束"
        for _ in range(6):
            t = ws.receive_json()["type"]
            types.append(t)
            if t in ("aiMove", "scoring"):
                break
    assert "thinking" in types
    assert "analysis" in types
    assert "aiMove" in types or "scoring" in types


def test_scoring_and_confirm_via_manager(client, auth):
    """终局结算：双方虚手 → ownership 判死子 → 确认数子 → 升降级 + 复盘入队。"""
    import asyncio

    from app.database import SessionLocal
    from app.game.manager import get_hub
    from app.game.rules import BLACK, WHITE
    from app.models import User

    _, headers, user_data = auth
    hub = get_hub()

    with SessionLocal() as db:
        user = db.get(User, user_data["id"])
        live = hub.create(user, size=9, komi=5.5, player_color=BLACK)
        gid = live.id
        # 直接摆出一个已分好地域的局面，再双方虚手
        live.game.board.place(BLACK, [(2, y) for y in range(9)])
        live.game.board.place(WHITE, [(6, y) for y in range(9)])
        live.game.moves = []
        live.game.play(BLACK, None)
        live.game.play(WHITE, None)
        assert live.game.board.is_over()

        ev = asyncio.run(hub.enter_scoring(live))
        assert ev["type"] == "scoring"
        assert "preview" in ev and "deadStones" in ev

        end = hub.confirm_score(live, [])
        assert end["type"] == "gameEnd"
        assert end["reason"] == "pass-pass"
        assert end["result"]["method"] == "area"
        # 黑围住 x=0..1（18 点）+ 9 子，白围住 x=7..8（18 点）+ 9 子 + 贴目 5.5
        assert end["result"]["blackTotal"] == 27
        assert end["result"]["whiteTotal"] == 32.5
        assert end["winner"] == WHITE
        assert end["playerWon"] is False
        assert end["sgf"].startswith("(;FF[4]")
        assert end["rank"]["progress"]["totalGames"] == 1
        assert live.phase == "finished"

    # 复盘已入队，轮询等待完成
    deadline = time.time() + 60
    while time.time() < deadline:
        st = client.get(f"/api/reviews/{gid}/status", headers=headers).json()
        if st["status"] in ("done", "failed"):
            break
        time.sleep(0.5)
    assert st["status"] == "done", f"终局对局应自动完成复盘：{st}"

    detail = client.get(f"/api/games/{gid}", headers=headers).json()["game"]
    assert detail["finished"] is True
    assert detail["resultText"]


def test_resume_from_scoring(client, auth):
    """结算阶段可反悔继续对局（撤销两次虚手）。"""
    import asyncio

    from app.database import SessionLocal
    from app.game.manager import get_hub
    from app.game.rules import BLACK, WHITE
    from app.models import User

    _, _, user_data = auth
    hub = get_hub()
    with SessionLocal() as db:
        user = db.get(User, user_data["id"])
        live = hub.create(user, size=9, komi=5.5, player_color=BLACK)
        live.game.play(BLACK, None)
        live.game.play(WHITE, None)
        asyncio.run(hub.enter_scoring(live))
        assert live.phase == "scoring"
        ev = hub.resume_from_scoring(live)
        assert ev["type"] == "resume"
        assert live.phase == "playing"
        assert len(live.game.moves) == 0
        assert live.game.next_color == BLACK
        # 收尾：认输结束对局，避免残留活对局影响其他用例
        hub.player_resign(live)


def test_resign_then_review_report(client, auth):
    token, headers, _ = auth
    gid = client.post("/api/games", json={"size": 9, "komi": 5.5, "hintMode": False},
                      headers=headers).json()["game"]["id"]

    # 先下几手，让复盘有内容可分析
    with client.websocket_connect(f"/ws/game/{gid}?token={token}") as ws:
        ws.receive_json()
        for i, (x, y) in enumerate([(2, 6), (6, 6), (2, 2), (6, 2)]):
            ws.send_json({"action": "move", "x": x, "y": y})
            for _ in range(6):
                msg = ws.receive_json()
                if msg["type"] == "aiMove":
                    break
        ws.send_json({"action": "resign"})
        for _ in range(6):
            msg = ws.receive_json()
            if msg["type"] == "gameEnd":
                assert msg["playerWon"] is False
                break

    # 复盘由后台 worker 异步生成，轮询等待
    deadline = time.time() + 60
    status = {}
    while time.time() < deadline:
        status = client.get(f"/api/reviews/{gid}/status", headers=headers).json()
        if status["status"] in ("done", "failed"):
            break
        time.sleep(0.5)
    assert status["status"] == "done", f"复盘应成功完成：{status}"

    data = client.get(f"/api/reviews/{gid}", headers=headers).json()
    report = data["report"]
    assert report is not None
    assert report["engine"] == "heuristic"
    assert report["totalMoves"] >= 4
    assert len(report["moves"]) == report["totalMoves"]
    assert report["curve"], "必须有胜率曲线数据"
    assert {"opening", "middle", "endgame", "overall", "training"} <= set(report["summary"])
    assert report["llm"]["used"] is False       # 未配置 Key → 模板降级
    assert report["llm"]["error"]
    # 每手报告结构完整
    m = report["moves"][0]
    for key in ("moveNum", "color", "isPlayer", "gtp", "flag", "flagLabel"):
        assert key in m
    # 损失目数必须算得出来（此前启发式引擎下全为 0，吻合度也跟着失真）
    player_losses = [it["lossPoints"] for it in report["moves"] if it["isPlayer"]]
    assert player_losses and all(v is not None for v in player_losses)
    assert report["avgLossPoints"] is not None

    md = client.get(f"/api/reviews/{gid}/export", headers=headers)
    assert md.status_code == 200
    assert "围棋复盘报告" in md.text
    assert "分阶段数据" in md.text

    # 手动重跑复盘
    rerun = client.post(f"/api/reviews/{gid}", headers=headers)
    assert rerun.status_code == 200


def test_import_sgf_creates_reviewable_game(client, auth):
    _, headers, _ = auth
    sgf = ("(;FF[4]GM[1]SZ[9]KM[5.5]PB[小明]PW[三段棋匠]RE[W+R]"
           ";B[cc];W[gg];B[cg];W[gc];B[fg];W[cf])")
    r = client.post("/api/games/import", json={"sgf": sgf, "playerColor": 1}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["moveCount"] == 6 and body["size"] == 9

    gid = body["gameId"]
    deadline = time.time() + 60
    while time.time() < deadline:
        st = client.get(f"/api/reviews/{gid}/status", headers=headers).json()
        if st["status"] in ("done", "failed"):
            break
        time.sleep(0.5)
    assert st["status"] == "done"
    report = client.get(f"/api/reviews/{gid}", headers=headers).json()["report"]
    assert report["totalMoves"] == 6

    detail = client.get(f"/api/games/{gid}", headers=headers).json()["game"]
    assert detail["status"] == "finished"
    assert detail["resultText"] == "W+R" or detail["resultText"]
    assert detail["colorSource"] == "import", "导入棋谱的来源必须是 import"


def test_review_flags_obviously_bad_move(client, auth):
    """明显的恶手必须被标出来。

    这是“损失目数口径”的验收点：跳节点差值在启发式引擎下恒为 0，
    会把每一手都判成“好手”；同节点口径下 1-1 开局应被估出 2 目以上损失。
    """
    _, headers, _ = auth
    # 黑首手 1-1（SGF aa = 左上角 = GTP A9）是 9 路盘上公认的恶手，其余几手正常
    sgf = ("(;FF[4]GM[1]SZ[9]KM[5.5]PB[测试]PW[测试]RE[W+R]"
           ";B[aa];W[ee];B[cc];W[gg])")
    r = client.post("/api/games/import", json={"sgf": sgf, "playerColor": 1}, headers=headers)
    assert r.status_code == 200, r.text
    gid = r.json()["gameId"]

    deadline = time.time() + 60
    while time.time() < deadline:
        st = client.get(f"/api/reviews/{gid}/status", headers=headers).json()
        if st["status"] in ("done", "failed"):
            break
        time.sleep(0.5)
    assert st["status"] == "done", f"复盘应成功完成：{st}"

    report = client.get(f"/api/reviews/{gid}", headers=headers).json()["report"]
    first = report["moves"][0]
    assert first["gtp"].upper() == "A9" and first["isPlayer"]
    assert first["x"] == 0 and first["y"] == 8, "SGF 行序与 GTP 相反，aa 应落在左上角"
    assert first["lossPoints"] is not None and first["lossPoints"] > 2.0
    assert first["flag"] in ("slow", "bad", "blunder"), f"1-1 开局不应被判为好手：{first}"
    assert report["avgLossPoints"] and report["avgLossPoints"] > 0
    assert report["counts"]["good"] < report["totalMoves"]
    # 关键手列表要包含这一手，否则 LLM / 模板讲解就没有对象
    assert 1 in report["keyMoves"]


def test_game_list_and_timeline(client, auth):
    _, headers, _ = auth
    # 先下一局再认输，确保列表里有记录
    gid = client.post("/api/games", json={"size": 9, "komi": 5.5},
                      headers=headers).json()["game"]["id"]
    client.post(f"/api/games/{gid}/resign", headers=headers)

    games = client.get("/api/games", headers=headers).json()
    assert games["total"] >= 1
    assert all("rankName" in it for it in games["items"])
    assert any(it["id"] == gid for it in games["items"])
    tl = client.get("/api/auth/me/timeline", headers=headers).json()
    assert "items" in tl and tl["rankRange"]["max"] == 27


def test_llm_config_roundtrip(client, auth):
    _, headers, user = auth
    r = client.put("/api/auth/me/llm", json={
        "baseUrl": "https://api.deepseek.com/v1", "apiKey": "sk-test-123",
        "model": "deepseek-chat"}, headers=headers)
    assert r.status_code == 200
    cfg = r.json()["llmConfig"]
    assert cfg["hasApiKey"] is True and cfg["model"] == "deepseek-chat"
    assert "sk-test" not in str(cfg), "API Key 不应回传明文"

    me = client.get("/api/auth/me", headers=headers).json()["user"]
    assert me["llmConfig"]["hasApiKey"] is True

    # 落库的必须是密文（审计 1.22）：`users.llm_config` 是明文 JSON 列，
    # 而库文件会进备份、可能被拷走，API Key 不该在那里裸奔。
    stored = _stored_api_key(user["username"])
    assert stored.startswith("enc:v1:"), f"库里仍是明文：{stored!r}"
    assert "sk-test" not in stored
    assert unseal(stored) == "sk-test-123", "密文必须能解回原值"

    # 清除 Key
    r2 = client.put("/api/auth/me/llm", json={"apiKey": ""}, headers=headers)
    assert r2.json()["llmConfig"]["hasApiKey"] is False
    assert _stored_api_key(user["username"]) == "", "清除后库里不该留残余"


def test_review_report_legacy_keys_are_normalized():
    """改名前的旧报告（accuracy / aiAccuracy）在**读出口**统一翻成新键（§3.16）。

    值是目/手（平均每手损失目数）而不是百分比 —— 键名不再叫 accuracy，
    免得前端按吻合度百分比渲染。已落库的旧报告仍带旧键，这里做兼容。
    """
    from app.api.reviews import _normalize_report

    new = _normalize_report({"accuracy": 3.5, "aiAccuracy": 4.0, "totalMoves": 12})
    assert new["avgLossPoints"] == 3.5 and new["aiAvgLossPoints"] == 4.0
    assert new["totalMoves"] == 12
    assert "accuracy" not in new and "aiAccuracy" not in new

    assert _normalize_report(None) is None
    # 已经是新键的报告原样返回
    assert _normalize_report({"avgLossPoints": 1.2}) == {"avgLossPoints": 1.2}


def _stored_api_key(username: str) -> str:
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import User
    with SessionLocal() as s:
        row = s.scalar(select(User).where(User.username == username))
        return (row.llm_config or {}).get("apiKey", "")


def test_profile_preferences_update(client, auth):
    _, headers, _ = auth
    r = client.patch("/api/auth/me", json={"displayName": "棋童小明", "hintMode": False,
                                           "demotionEnabled": True}, headers=headers)
    assert r.status_code == 200
    u = r.json()["user"]
    assert u["displayName"] == "棋童小明"
    assert u["hintMode"] is False
    assert u["demotionEnabled"] is True


def test_hint_mode_patch_reaches_the_live_game(client, auth):
    """设置页改「落子推荐」必须当场推进进行中的对局，不是下一局才生效。

    活对局的 `hint_mode` 是开局快照；PATCH /api/auth/me 只落库不广播的话，
    用户报的「关了推荐点还一直显示」会一直成立到重开一局。
    """
    token, headers, _ = auth
    gid = client.post("/api/games", json={"size": 9, "komi": 5.5, "hintMode": True},
                      headers=headers).json()["game"]["id"]
    with client.websocket_connect(f"/ws/game/{gid}?token={token}") as ws:
        ws.receive_json()                                   # 首帧 state
        r = client.patch("/api/auth/me", json={"hintMode": False}, headers=headers)
        assert r.status_code == 200 and r.json()["user"]["hintMode"] is False
        ev = ws.receive_json()
        assert ev == {"type": "hintMode", "enabled": False}, ev
        r = client.patch("/api/auth/me", json={"hintMode": True}, headers=headers)
        assert r.status_code == 200
        ev = ws.receive_json()
        assert ev == {"type": "hintMode", "enabled": True}, ev


# ---------------------------------------------------------------- 每手限时


def test_move_clock_times_out_an_idle_player(client, auth):
    """到点不落子判超时负，而且不需要客户端在线（服务端看门狗负责）。

    玩家直接关标签页时 WS 那条「落子时查超时」的路径永远不执行，
    没有看门狗的话对局会永远挂在「进行中」并占着新开对局的名额。
    """
    _, headers, _ = auth
    gid = client.post("/api/games", json={"size": 9, "komi": 5.5, "moveSeconds": 1},
                      headers=headers).json()["game"]["id"]
    g = client.get(f"/api/games/{gid}", headers=headers).json()["game"]
    assert g["moveSeconds"] == 1 and g["moveSecondsLeft"] == 1

    deadline = time.time() + 10
    while time.time() < deadline:
        g = client.get(f"/api/games/{gid}", headers=headers).json()["game"]
        if g["finished"]:
            break
        time.sleep(0.3)
    assert g["finished"] is True, "限时 1 秒的对局应在几秒内被看门狗判超时"
    assert g["finishReason"] == "timeout"
    assert g["playerWon"] is False and g["winner"] == 2
    assert "超时" in g["resultText"]
    # 超时是一场真实的负局：计入战绩
    me = client.get("/api/auth/me", headers=headers).json()["user"]["progress"]
    assert me["totalGames"] >= 1 and me["totalWins"] == 0


def test_move_clock_resets_each_turn_and_can_be_disabled(client, auth):
    token, headers, _ = auth
    gid = client.post("/api/games", json={"size": 9, "komi": 5.5, "moveSeconds": 30},
                      headers=headers).json()["game"]["id"]
    with client.websocket_connect(f"/ws/game/{gid}?token={token}") as ws:
        ws.receive_json()                                    # state
        ws.send_json({"action": "move", "x": 2, "y": 6})
        left_after_ai = None
        for _ in range(6):
            msg = ws.receive_json()
            if msg["type"] == "aiMove":
                left_after_ai = msg.get("moveSecondsLeft")
                break
        # AI 落完子又轮到玩家：新一轮 30 秒重新开始
        assert left_after_ai is not None and 1 <= left_after_ai <= 30
        g = client.get(f"/api/games/{gid}", headers=headers).json()["game"]
        assert not g["finished"]
        client.post(f"/api/games/{gid}/resign", headers=headers)

    # moveSeconds=0 表示不限时：不下发倒计时
    off = client.post("/api/games", json={"size": 9, "moveSeconds": 0},
                      headers=headers).json()["game"]
    assert off["moveSeconds"] == 0 and off["moveSecondsLeft"] is None
    client.post(f"/api/games/{off['id']}/resign", headers=headers)

    # 不传则用全局默认（宽松值，而不是 0）
    dflt = client.post("/api/games", json={"size": 9}, headers=headers).json()["game"]
    assert dflt["moveSeconds"] == settings.move_seconds_default > 0
    client.post(f"/api/games/{dflt['id']}/resign", headers=headers)


def test_move_seconds_survive_a_restart(client, auth):
    """限时是开局时定的，服务重启（内存里没这局）后恢复出来还得是同一个值。"""
    from app.game.manager import get_hub

    _, headers, _ = auth
    gid = client.post("/api/games", json={"size": 9, "moveSeconds": 120},
                      headers=headers).json()["game"]["id"]
    get_hub().remove(gid)                       # 模拟重启
    g = client.get(f"/api/games/{gid}", headers=headers).json()["game"]
    assert g["moveSeconds"] == 120
    # 恢复出的活对局应重新给玩家一手的时间
    assert g["moveSecondsLeft"] is not None and g["moveSecondsLeft"] <= 120
    client.post(f"/api/games/{gid}/force-end", headers=headers)


def test_add_missing_columns_is_idempotent():
    """旧库升级只能加列；重复启动不能报错，也不能动已有数据。"""
    from app.database import _add_missing_columns, engine

    _add_missing_columns()
    _add_missing_columns()
    with engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(games)")}
    assert "move_seconds" in cols
    # 复盘进度三列也得补上，否则旧库上第一次复盘就炸
    assert {"review_progress", "review_stage", "review_detail"} <= cols
    # L15：执子来源列（老库补上默认自选）
    assert "color_source" in cols


def test_review_progress_mapping_is_monotonic():
    """阶段→百分比的映射必须单调且封顶在 1.0，否则进度条会往回跳。"""
    from app.review.worker import (STAGE_ANALYZE, STAGE_COMMENT, STAGE_DONE,
                                   STAGE_ENGINE, STAGE_QUEUED, STAGE_REPORT,
                                   STAGE_TEXT, _frac)

    stages = [STAGE_QUEUED, STAGE_ENGINE, STAGE_ANALYZE, STAGE_REPORT,
              STAGE_COMMENT, STAGE_DONE]
    ends = [_frac(s, 1.0) for s in stages]
    assert ends == sorted(ends), f"阶段结束值必须递增：{ends}"
    assert ends[0] >= 0.0 and ends[-1] == 1.0
    assert _frac(STAGE_ANALYZE, 0.0) < _frac(STAGE_ANALYZE, 0.5) < _frac(STAGE_ANALYZE, 1.0)
    assert _frac(STAGE_ANALYZE, 9.0) == _frac(STAGE_ANALYZE, 1.0), "比例要 clamp"
    assert all(s in STAGE_TEXT for s in stages + ["failed"]), "每个阶段都要有人读文案"


def test_review_reports_progress_until_done(client, auth):
    """复盘不能只有 pending/done 两态：阶段、百分比、描述都要查得到。

    这是「复盘看上去卡死」的验收点：一盘 9 路棋要跑几十秒（等 KataGo 预热时
    更久），前端得能把「走到哪一步」画出来。

    用导入棋谱而不是 WS 真下一盘：后者要靠 AI 不碰到测试要下的点，
    一旦碰到就只回 error 事件，而等 aiMove 的接收循环会永远阻塞在那里。
    """
    _, headers, _ = auth
    sgf = ("(;FF[4]GM[1]SZ[9]KM[5.5]PB[小明]PW[入门学徒]RE[W+R]"
           ";B[cc];W[gg];B[cg];W[gc];B[fg];W[cf])")
    r = client.post("/api/games/import", json={"sgf": sgf, "playerColor": 1}, headers=headers)
    assert r.status_code == 200, r.text
    gid = r.json()["gameId"]

    st = {}
    deadline = time.time() + 60
    while time.time() < deadline:
        st = client.get(f"/api/reviews/{gid}/status", headers=headers).json()
        assert 0.0 <= st["progress"] <= 1.0, st
        assert st["stageText"], "阶段必须带人读文案"
        if st["status"] in ("done", "failed"):
            break
        time.sleep(0.2)
    assert st["status"] == "done", st
    assert st["stage"] == "done" and st["progress"] == 1.0

    # 列表与详情里也带进度，大厅不必为每一行再拉一次 status
    brief = {i["id"]: i for i in
             client.get("/api/games", headers=headers).json()["items"]}[gid]
    assert brief["reviewProgress"] == 1.0 and brief["reviewStage"] == "done"
    meta = client.get(f"/api/reviews/{gid}", headers=headers).json()["meta"]
    assert meta["reviewProgress"] == 1.0 and meta["reviewStage"] == "done"

    # 重跑复盘时进度要归零重新走（否则前端会看到停在 100% 却在转圈）
    assert client.post(f"/api/reviews/{gid}", headers=headers).status_code == 200
    st2 = {}
    while time.time() < deadline:
        st2 = client.get(f"/api/reviews/{gid}/status", headers=headers).json()
        if st2["status"] in ("done", "failed"):
            break
        assert st2["progress"] < 1.0 or st2["stage"] == "done", st2
        time.sleep(0.2)
    assert st2["status"] == "done" and st2["progress"] == 1.0


# ---------------------------------------------------------------------------
# 结构性约束：路由的同步 / 异步声明
# ---------------------------------------------------------------------------
def _is_route_decorator(decorator) -> bool:
    """@router.get("/x") / @app.post("/x") 这类路由装饰器（websocket 也算）。"""
    if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
        return False
    holder = decorator.func.value
    return (isinstance(holder, ast.Name) and holder.id in ("router", "app")
            and decorator.func.attr in ("get", "post", "put", "patch", "delete", "websocket"))


def test_route_handlers_without_await_are_declared_sync():
    """路由函数体内没有 await，就必须声明成 `def`，不能是 `async def`。

    FastAPI 对 `async def` 路由是**直接在事件循环里调用**的，只有 `def` 路由才丢线程池。
    实测踩过的坑：`/api/tsumego/{id}/attempt` 曾是 async def，而玩家走变化线之外的一手时
    它要跑三次 DEPTH=28 的局部穷举搜索——搜索期间**整个服务卡住**，别人的对局
    WebSocket 消息与所有接口请求全部排队。这类问题在单用户测试下完全看不出来。

    只检查带路由装饰器的函数：普通 async 函数可能被 `await` 调用，改成 def 会直接坏
    （例如 manager.restore、review.worker 里的那几个，它们要改用 run_in_threadpool 包重活）。
    """
    # 不能用 app.__file__：本文件里 `app` 是 `from app.main import app` 的 **FastAPI 实例**，
    # 不是模块。从测试文件自身往上推：tests/ 的上一级就是 backend/。
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    scanned = 0
    for path in sorted(app_dir.rglob("*.py")):
        scanned += 1
        # utf-8-sig：app 下曾有 7 个 __init__.py 带 BOM（已清理），而 ast.parse 碰到
        # U+FEFF 会直接 SyntaxError；继续用 utf-8-sig 解码，将来再混进带 BOM 的文件也不会红。
        tree = ast.parse(path.read_bytes().decode("utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            if not any(_is_route_decorator(d) for d in node.decorator_list):
                continue
            body = list(ast.walk(node))
            if any(isinstance(n, (ast.Await, ast.AsyncFor, ast.AsyncWith)) for n in body):
                continue
            offenders.append(f"{path.name}:{node.lineno} {node.name}")
    # 防「假绿」：路径算错时 rglob 扫不到任何文件，offenders 自然为空，测试会白白通过
    assert scanned > 20, f"只扫到 {scanned} 个源文件，路径大概算错了：{app_dir}"
    assert not offenders, (
        "这些路由声明成 async def 但体内没有任何 await，同步阻塞会卡住事件循环，"
        f"应改为 def：{offenders}")
