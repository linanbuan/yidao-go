"""对局 WebSocket：落子、悔棋、认输、终局结算、实时分析推送。

协议（客户端 → 服务端）：
  {"action":"move","x":3,"y":15}     落子
  {"action":"pass"}                  虚手
  {"action":"takeback","plies":2}    悔棋（默认 2 手 = 玩家+AI）
  {"action":"resign"}                投子认输
  {"action":"scoreConfirm","dead":[[x,y],...]}  确认终局（可修正死子）
  {"action":"resume"}                撤销结算，继续对局
  {"action":"hint"}                  主动索取当前局面提示
  {"action":"ping"}                  心跳

协议（服务端 → 客户端）：
  state / move / aiMove / analysis(+hint) / scoring / gameEnd / takeback /
  reviewReady / thinking / error / pong
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..database import SessionLocal
from ..engine.pool import get_pool
from ..game.manager import get_hub
from ..game.rules import IllegalMove
from ..models import GameRecord, User
from ..security import user_id_from_token
from ..config import settings

logger = logging.getLogger("go.ws")
router = APIRouter()


@router.websocket("/ws/game/{game_id}")
async def ws_game(ws: WebSocket, game_id: str, token: str = ""):
    await ws.accept()
    user_id = user_id_from_token(token)
    if not user_id:
        await ws.send_json({"type": "error", "message": "未登录或登录已过期"})
        await ws.close(code=4401)
        return

    with SessionLocal() as db:
        user = db.get(User, user_id)
        rec = db.get(GameRecord, game_id)
        if user is None or rec is None or rec.user_id != user.id:
            await ws.send_json({"type": "error", "message": "无权访问该对局"})
            await ws.close(code=4403)
            return
        hint_mode = bool(user.hint_mode)

    hub = get_hub()
    live = hub.get(game_id) or await hub.restore(game_id, hint_mode)
    if live is None:
        await ws.send_json({"type": "error", "message": "对局不存在或已结束"})
        await ws.close(code=4404)
        return

    outgoing: asyncio.Queue = asyncio.Queue()
    listener = lambda ev: outgoing.put_nowait(ev)
    live.bind(listener)

    async def sender() -> None:
        while True:
            msg = await outgoing.get()
            try:
                await ws.send_json(msg)
            except Exception:   # noqa: BLE001
                return

    sender_task = asyncio.create_task(sender())

    async def send(event: dict) -> None:
        await outgoing.put(event)

    try:
        await send({"type": "state", "state": live.snapshot()})

        # 结算阶段重连：死子判定未持久化，重新估算一次
        if live.phase == "scoring" and not live.scoring_dead:
            dead = await hub.estimate_dead_stones(live)
            live.scoring_dead = dead
            await send({"type": "scoring",
                        "deadStones": [[p[0], p[1]] for p in dead],
                        "preview": live.game.board.score(live.game.komi,
                                                         live.game.score_method, dead),
                        "message": "已重新判定死子，请确认后结束对局。"})

        # 轮到 AI 走（例如断线时 AI 还没落子）
        if live.phase == "playing" and live.game.next_color != live.player_color:
            await send({"type": "thinking", "aiName": live.ai_name})
            await _ai_turn(hub, live, send)

        while True:
            try:
                data = await ws.receive_json()
            except WebSocketDisconnect:
                raise
            except Exception:   # noqa: BLE001
                # 非 JSON 载荷（客户端/代理发错）：回一条 error 继续收，别整条连接陪葬
                await send({"type": "error", "message": "消息不是合法 JSON"})
                continue
            if not isinstance(data, dict):
                await send({"type": "error", "message": "消息格式不正确"})
                continue
            try:
                await _dispatch(hub, live, data, send)
            except WebSocketDisconnect:
                raise
            except Exception as exc:   # noqa: BLE001
                # 单条指令出错不该拆掉整条连接（旧实现一发 error 就退出循环关连接，
                # 重连后若在结算阶段，死子修正会被重估丢弃）。
                logger.exception("WebSocket 指令处理失败: %s", exc)
                await send({"type": "error", "message": "请求处理失败，请重试"})

    except WebSocketDisconnect:
        logger.info("对局 %s 的 WebSocket 断开（状态已落库，可重连）", game_id)
    except Exception as exc:   # noqa: BLE001
        logger.exception("WebSocket 异常: %s", exc)
        try:
            # 不回传异常原文（可能含上游/内部细节），详情只进服务端日志
            await send({"type": "error", "message": "服务端异常，请稍后重试"})
        except Exception:   # noqa: BLE001
            pass
    finally:
        live.unbind(listener)
        sender_task.cancel()
        try:
            await sender_task
        except (asyncio.CancelledError, Exception):   # noqa: BLE001
            pass


def _parse_point(data: dict, size: int) -> tuple[int, int]:
    """解析落子坐标：类型与范围都要拦在前面。

    旧实现把 `int(x)` 放在 try/except IllegalMove **之外**，客户端发
    `{"x":"abc"}` 或 `{"x":null}` 都会抛 ValueError/TypeError 把整条连接打崩。
    """
    x, y = data.get("x"), data.get("y")
    for v in (x, y):
        if isinstance(v, bool) or not isinstance(v, int):
            raise IllegalMove("坐标必须是整数")
    if not (0 <= x < size and 0 <= y < size):
        raise IllegalMove("落点超出棋盘")
    return int(x), int(y)


def _parse_dead(raw, size: int) -> Optional[list[tuple[int, int]]]:
    """解析死子列表：形状不对一律当作非法请求（不抛 IndexError/ValueError）。"""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise IllegalMove("dead 必须是坐标数组")
    out: list[tuple[int, int]] = []
    for p in raw:
        if isinstance(p, (str, bytes)) or not hasattr(p, "__len__") or len(p) != 2:
            raise IllegalMove("死子坐标必须是 [x, y] 形式")
        x, y = p[0], p[1]
        if isinstance(x, bool) or isinstance(y, bool) or \
                not isinstance(x, int) or not isinstance(y, int):
            raise IllegalMove("死子坐标必须是整数")
        if not (0 <= x < size and 0 <= y < size):
            raise IllegalMove(f"死子坐标越界：({x}, {y})")
        out.append((int(x), int(y)))
    return out


async def _dispatch(hub, live, data: dict, send) -> None:
    """处理单条客户端指令（与 `game_id` 的循环解耦，便于逐条兜底异常）。"""
    action = str(data.get("action", "")).lower()

    if action == "ping":
        await send({"type": "pong"})
        return

    if live.phase == "finished":
        await send({"type": "error", "message": "对局已结束"})
        return

    # 超时以服务端时钟为准：玩家可能在倒计时归零后才把落子发过来
    expired = await hub.expire_if_due(live)
    if expired is not None:
        await send(expired)
        return

    if action in ("move", "pass"):
        if live.phase != "playing":
            await send({"type": "error", "message": "当前不在对局阶段"})
            return
        point = None
        if action == "move":
            try:
                point = _parse_point(data, live.game.size)
            except IllegalMove as exc:
                await send({"type": "error", "message": str(exc)})
                return
        try:
            res = await hub.player_move(live, point)
        except IllegalMove as exc:
            await send({"type": "error", "message": str(exc)})
            return
        for ev in res["events"]:
            await send(ev)
        return

    if action == "takeback":
        raw = data.get("plies", 2)
        if isinstance(raw, bool) or not isinstance(raw, int):
            await send({"type": "error", "message": "悔棋手数必须是整数"})
            return
        try:
            ev = await hub.takeback(live, raw)
            await send(ev)
        except IllegalMove as exc:
            await send({"type": "error", "message": str(exc)})
        return

    if action == "resign":
        await send(hub.player_resign(live))
        return

    if action == "scoreconfirm":
        if live.phase != "scoring":
            await send({"type": "error", "message": "当前不在终局结算阶段"})
            return
        try:
            dead = _parse_dead(data.get("dead"), live.game.size)
        except IllegalMove as exc:
            await send({"type": "error", "message": str(exc)})
            return
        try:
            await send(hub.confirm_score(live, dead))
        except IllegalMove as exc:
            await send({"type": "error", "message": str(exc)})
        return

    if action == "resume":
        try:
            await send(hub.resume_from_scoring(live))
        except IllegalMove as exc:
            await send({"type": "error", "message": str(exc)})
        return

    if action == "hint":
        # 关掉落子推荐的玩家仍可主动索取提示，但语义上先检查一下模式
        if not live.hint_mode:
            await send({"type": "error", "message": "提示功能已关闭，可在设置页开启"})
            return
        await _hint_now(live, send)
        return

    await send({"type": "error", "message": f"未知指令: {action}"})


async def _ai_turn(hub, live, send) -> None:
    """在轮到 AI 时补一手（重连恢复用）。

    与 player_move 共用同一把锁：两个连接同时重连（或一个在重连、另一个在落子）
    时，AI 回合只会执行一次，不会出现「AI 连走两手」。
    """
    async with live.lock:
        if live.phase != "playing" or live.game.next_color == live.player_color:
            # 等锁的这几秒里局面已经变了（例如另一条连接补完了 AI 手）：
            # 推一份全量 state 让这个客户端追上，而不是再下一手
            await send({"type": "state", "state": live.snapshot()})
            return
        try:
            query = live.build_query()
            result = await get_pool().analyze(query, side_to_move=live.ai_color,
                                              turn=len(live.game.moves), profile=live.profile)
            if live.abandoned or live.phase != "playing":
                # await 引擎的这几秒里对局可能被强制结束或记录被删。再往下走就会
                # 调 finish("ai-resign") → _apply_rank，给玩家凭空记上一胜。
                return
            live.record_analysis(result)
            await send({"type": "analysis", "index": len(live.game.moves),
                        "analysis": live.analyses[-1]})
            if live.update_despair(result):
                await send(await hub.finish(live, reason="ai-resign"))
                hub.save(live)
                return
            point = get_pool().choose_move(live.game.board, live.ai_color, result, live.profile)
            move = live.game.play(live.ai_color, point)
            # 与 player_move 一致：先 arm 玩家计时，再取剩余秒数随事件下发
            hub.arm_player_clock(live)
            await send({"type": "aiMove", "move": move.to_dict(live.game.size),
                        "moveCount": len(live.game.moves),
                        "moveSecondsLeft": live.seconds_left()})
            if live.game.finished:
                await send(await hub.enter_scoring(live))
            hub.save(live)
            live.spawn(hub._analyze_after_ai(live, len(live.game.moves)))
        except IllegalMove as exc:
            await send({"type": "error", "message": str(exc)})
        except Exception as exc:   # noqa: BLE001
            logger.exception("AI 走子失败: %s", exc)
            await send({"type": "error", "message": "AI 走子失败，请重试"})


async def _hint_now(live, send) -> None:
    """玩家主动索取提示（不影响对局状态）。

    提示必须是「最强引擎最优解」口径，而不是档位的人风格采样：
    玩家照着提示下，复盘（主网络 + review_visits）用的就是同一把尺——
    现场问题：human 模型提示点与复盘首选不同，玩家完全按提示下复盘仍被报大损失。

    `profile` 必须传 None（第 39 轮审计确认）：`pool.analyze` 收到非 None 的档位
    profile 会无条件把 `max_visits` 与 human 模型**覆写回查询**——§31 只改了
    build_query 的初值，真实路径上从未生效（级位档全部 human=True）。不传 profile
    与复盘（analyze_turns）同口径：主网络 + review_visits，不会被任何覆写。
    """
    try:
        query = live.build_query(max_visits=settings.review_visits)
        query.human_sl_profile = ""
        # side_to_move 必须是局面实际的待走方（= next_color）：ownership 的
        # 符号换算以它为基准。AI 思考期间点提示时局面轮到 AI，若仍传 player_color，
        # 领地热力图会整盘黑白颠倒
        result = await get_pool().analyze(query, side_to_move=live.game.next_color,
                                          turn=len(live.game.moves))
        await send({
            "type": "hintOnly",
            "hint": [c.to_dict(live.game.size) for c in result.candidates[:3]],
            "winrateBlack": round(result.winrate_black, 4),
            "scoreLead": round(result.score_lead, 2),
            "ownership": [round(v, 3) for v in result.ownership_black_view(live.game.size)],
        })
    except Exception as exc:   # noqa: BLE001
        logger.exception("提示获取失败: %s", exc)
        await send({"type": "error", "message": "提示获取失败，请重试"})
