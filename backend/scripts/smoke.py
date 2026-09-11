"""端到端自检脚本：对运行中的后端跑一遍完整流程。

用法（先启动后端 `python -m uvicorn app.main:app --port 8000`，再执行）：
    python scripts/smoke.py                     # 默认 http://127.0.0.1:8000
    python scripts/smoke.py --base http://host:8000 --moves 12

覆盖：注册登录 → 开局 → WebSocket 对局（落子/提示/悔棋）→ 认输 →
      复盘生成 → 报告导出 → 段位进度校验。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def step(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def ok(msg: str) -> None:
    print(f"  [OK] {msg}", flush=True)


def info(msg: str) -> None:
    print(f"  [..] {msg}", flush=True)


async def recv_until(ws, wanted: set[str], limit: int = 8, timeout: float = 30.0):
    """持续读消息直到出现期望类型。

    必须带超时：服务端不一定会为每个指令回复固定数量的消息，
    无超时的 recv() 在异常分支下会永久阻塞（曾经让本脚本假死）。
    返回 (收集到的消息列表, 是否超时)。
    """
    got: list[dict] = []
    for _ in range(limit):
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return got, True
        msg = json.loads(raw)
        got.append(msg)
        if msg.get("type") in wanted:
            return got, False
    return got, False


def next_empty_point(board: list[list[int]], size: int, prefer: int = 3) -> tuple[int, int] | None:
    """从当前局面里按“三、四线优先”挑一个空点（避开与 AI 落点冲突）。"""
    ordered: list[tuple[int, int]] = []
    for y in range(size):
        for x in range(size):
            if board[y][x] != 0:
                continue
            line = min(x, y, size - 1 - x, size - 1 - y) + 1
            ordered.append((abs(line - prefer), -(x * size + y), x, y))
    ordered.sort()
    return (ordered[0][2], ordered[0][3]) if ordered else None


def apply_move(board: list[list[int]], mv: dict) -> None:
    """在本地棋盘上复现一手（含提子），用于挑选不冲突的下一手。"""
    if mv.get("x") is None or mv.get("y") is None:
        return
    board[mv["y"]][mv["x"]] = mv["color"]
    for cx, cy in mv.get("captures") or []:
        board[cy][cx] = 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--size", type=int, default=9)
    ap.add_argument("--moves", type=int, default=8, help="玩家落子手数")
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()
    base = args.base.rstrip("/")

    try:
        import websockets   # noqa: F401  由 uvicorn[standard] 提供
    except ImportError:
        print("缺少 websockets 依赖：pip install websockets")
        return 2

    async with httpx.AsyncClient(base_url=base, timeout=30) as c:
        # ---------- 健康检查 ----------
        step("健康检查")
        r = await c.get("/api/health")
        assert r.status_code == 200, r.text
        ok(f"服务在线：{r.json()}")

        r = await c.get("/api/ranks")
        ranks = r.json()["items"]
        assert len(ranks) == 27, f"段位表应为 27 级，实际 {len(ranks)}"
        ok(f"段位表 {ranks[0]['name']} → {ranks[-1]['name']}（共 {len(ranks)} 级）")

        # ---------- 注册登录 ----------
        step("注册与登录")
        username = f"smoke{int(time.time())}"
        r = await c.post("/api/auth/register", json={
            "username": username, "password": "smoke123456", "displayName": "自检棋童"})
        assert r.status_code == 200, r.text
        token = r.json()["token"]
        user = r.json()["user"]
        headers = {"Authorization": f"Bearer {token}"}
        ok(f"注册成功：{username}，初始段位 {user['progress']['rankName']}")

        r = await c.get("/api/system/status", headers=headers)
        st = r.json()
        engine = st["engine"]["active"]
        info(f"当前引擎：{engine}"
             + ("" if engine == "katago" else f"（{st['engine']['katago']['error']}）"))
        info(f"大模型：{'已配置 ' + st['llm']['model'] if st['llm']['configured'] else '未配置 → 复盘使用模板讲解'}")

        # ---------- 开局 ----------
        step("开始新对局")
        r = await c.post("/api/games", json={"size": args.size, "komi": 5.5,
                                             "playerColor": 1, "hintMode": True},
                         headers=headers)
        assert r.status_code == 200, r.text
        game = r.json()["game"]
        gid = game["id"]
        ok(f"对局 {gid[:8]}…：{game['size']}路，对手 {game['aiName']}（{game['rankName']}）")

        # ---------- WebSocket 对局 ----------
        step("WebSocket 对局")
        import websockets

        ws_url = base.replace("http://", "ws://").replace("https://", "wss://") + \
            f"/ws/game/{gid}?token={token}"
        played = 0
        ai_moves = 0
        analyses = 0
        hints = 0
        async with websockets.connect(ws_url, max_size=None) as ws:
            msgs, timed_out = await recv_until(ws, {"state"}, timeout=40)
            assert not timed_out and msgs, "未收到初始状态"
            state = msgs[0]["state"]
            size = int(state["size"])
            board = [row[:] for row in (state.get("board") or [])]
            ok(f"已连接，收到全量状态（phase={state['phase']}，引擎={state.get('engine') or '—'}）")

            # 主动索取提示
            await ws.send(json.dumps({"action": "hint"}))
            msgs, timed_out = await recv_until(ws, {"hintOnly", "error"})
            assert not timed_out, "索取提示超时"
            if msgs[-1]["type"] == "hintOnly":
                hints += 1
                tops = [h["gtp"] for h in (msgs[-1].get("hint") or [])]
                ok(f"提示可用：推荐点 {tops}")

            # 落子若干手（每次都从当前局面选空点，避开与 AI 落点冲突）
            for i in range(args.moves):
                point = next_empty_point(board, size, prefer=3 if i % 2 == 0 else 4)
                if point is None:
                    info("棋盘已满，提前结束落子")
                    break
                x, y = point
                await ws.send(json.dumps({"action": "move", "x": x, "y": y}))
                msgs, timed_out = await recv_until(ws, {"aiMove", "error", "gameEnd"})
                assert not timed_out, f"第 {i + 1} 手等待 AI 回应超时"
                for m in msgs:
                    if m["type"] == "analysis":
                        analyses += 1
                        if m.get("hint"):
                            hints += 1
                    elif m["type"] == "move":
                        apply_move(board, m["move"])
                        played += 1
                    elif m["type"] == "aiMove":
                        apply_move(board, m["move"])
                        ai_moves += 1
                if msgs[-1]["type"] == "error":
                    info(f"落子 ({x},{y}) 被拒：{msgs[-1]['message']}")
                if msgs[-1]["type"] == "gameEnd":
                    info("对局已提前结束")
                    break
            ok(f"完成 {played} 个回合：AI 落子 {ai_moves} 次，收到分析 {analyses} 条，提示 {hints} 次")
            assert played > 0 and ai_moves > 0, "对局未能正常推进"

            # 悔棋
            await ws.send(json.dumps({"action": "takeback", "plies": 2}))
            msgs, timed_out = await recv_until(ws, {"takeback", "error"})
            assert not timed_out, "悔棋无响应"
            if msgs[-1]["type"] == "takeback":
                ok(f"悔棋成功，当前手数 {msgs[-1]['moveCount']}")
            else:
                info(f"悔棋被拒：{msgs[-1]['message']}")

            # 认输
            await ws.send(json.dumps({"action": "resign"}))
            msgs, timed_out = await recv_until(ws, {"gameEnd", "error"})
            assert not timed_out, "认输无响应"
            end = msgs[-1]
            assert end["type"] == "gameEnd", f"未收到终局事件：{end}"
            ok(f"认输生效：{end['resultText']}")
            ok(f"AI 寄语：{end['aiWords']}")
            prog = end["rank"]["progress"]
            info(f"段位进度：{prog['rankName']}，本级胜场 {prog['rankWins']}/{prog['winsRequired']}，"
                 f"总战绩 {prog['totalGames']} 局")
            assert end["sgf"].startswith("(;FF[4]"), "SGF 导出异常"
            ok(f"SGF 已生成（{len(end['sgf'])} 字节）")

        # ---------- 复盘 ----------
        step("AI 复盘")
        deadline = time.time() + args.timeout
        status = {}
        while time.time() < deadline:
            r = await c.get(f"/api/reviews/{gid}/status", headers=headers)
            status = r.json()
            if status["status"] in ("done", "failed"):
                break
            await asyncio.sleep(1)
        assert status.get("status") == "done", f"复盘未完成：{status}"
        ok(f"复盘完成（{status['status']}）")

        r = await c.get(f"/api/reviews/{gid}", headers=headers)
        rep = r.json()["report"]
        assert rep, "复盘报告为空"
        counts = rep["counts"]
        ok(f"吻合度：玩家 {rep['avgLossPoints']} 目/手，AI {rep['aiAvgLossPoints']} 目/手")
        ok(f"问题手：大恶手 {counts['blunder']}、恶手 {counts['bad']}、缓手 {counts['slow']}")
        ok(f"曲线点数：{len(rep['curve'])}，逐手报告：{len(rep['moves'])} 条")
        info(f"讲解来源：{'大模型 ' + rep['llm']['model'] if rep['llm']['used'] else '模板（' + rep['llm']['error'] + '）'}")
        print("\n  --- 总评 ---")
        print(f"  {rep['summary']['overall'][:220]}")
        if rep["summary"].get("training"):
            print("  --- 训练建议 ---")
            for t in rep["summary"]["training"][:3]:
                print(f"  · {t}")

        r = await c.get(f"/api/reviews/{gid}/export", headers=headers)
        assert r.status_code == 200 and "围棋复盘报告" in r.text
        ok(f"Markdown 报告导出成功（{len(r.text)} 字符）")

        # ---------- 历史与时间线 ----------
        step("战绩与成长记录")
        r = await c.get("/api/games", headers=headers)
        games = r.json()
        assert games["total"] >= 1
        ok(f"历史对局 {games['total']} 局，最近一局：{games['items'][0]['resultText']}")
        r = await c.get("/api/auth/me/timeline", headers=headers)
        ok(f"等级流水 {len(r.json()['items'])} 条")

    print("\n全部自检通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
