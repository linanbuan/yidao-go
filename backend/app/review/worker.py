"""赛后复盘 worker：异步队列 + 批量逐手分析 + LLM 讲解生成。

对局结束后立即入队返回（不阻塞前端），worker 在后台：
  1. 用更高的 visit 数重新逐手分析整盘棋（一次查询分析多手，效率高）
  2. 计算损失目数、问题手标记、吻合度、分阶段统计、转折点
  3. 把关键手分批喂给大模型生成中文讲解；LLM 不可用时用模板降级
  4. 写回 review_json，并通过 WebSocket 通知前端（前端也有轮询兜底）
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from ..config import settings
from ..database import SessionLocal
from ..engine.pool import get_pool
from ..engine.protocol import AnalysisQuery
from ..game.manager import get_hub
from ..game.rules import BLACK, Game, IllegalMove, WHITE, color_name
from ..llm.client import LLMClient, LLMError
from ..models import GameRecord, User
from ..rank.defs import get_rank
from . import analyzer, commentary
from sqlalchemy import select

logger = logging.getLogger("go.review")

_queue: Optional[asyncio.Queue] = None
_worker_task: Optional[asyncio.Task] = None
_worker_loop: Optional[asyncio.AbstractEventLoop] = None
#: 没有活着的 worker 时攒下的复盘请求（服务重启期间结束的对局），
#: 下一次 `start_worker()` 会把它们补进新队列 —— 见 `_orphaned` 的两个使用点。
_orphaned: list[str] = []
#: 当前正在跑的那一局（取消时要把它的状态交代清楚，见 stop_worker / _loop）。
_current: Optional[str] = None
_queue_maxsize = 200
CHUNK_TURNS = 20      # 每次查询分析的手数上限（限制单次响应体积）
#: 整块降级后拆成这么小的块重试一次，仍失败才接受降级。
RETRY_CHUNK_TURNS = 5
#: 分块查询超时 = 基础 + 每手增量。旧实现对「20 手一块」与「单局面」共用
#: config.katago_timeout=60：19 路无 GPU 时一块常在 60 秒内出不完，整块 20 手
#: 直接掉进启发式降级（而复盘报告仍以 katago 口径讲故事）。
CHUNK_TIMEOUT_BASE = 20.0
CHUNK_TIMEOUT_PER_TURN = 6.0


def _chunk_timeout(n_turns: int) -> float:
    return CHUNK_TIMEOUT_BASE + CHUNK_TIMEOUT_PER_TURN * max(1, n_turns)
# 存下来的候选点数：KataGo 返回多少候选完全由 visits 决定（没有 maxMoves 这个字段），
# review_visits=96 在 9 路上大约探 15~20 个点、19 路更多，24 能把它探到的都存下。
# 存少了 analyzer.node_loss 会找不到实际落点，只能退回跳节点估算
# （对 KataGo 而言跳节点也是可信的，所以是降级而不是出错）。
REVIEW_TOP_N = 24
# KataGo 还在后台预热时的等待上限：复盘是离线任务，等几十秒不伤对局体验，
# 而引擎差一档报告精度差很多（启发式的胜率/目差只能粗看）
REVIEW_ENGINE_WAIT = 180.0


def _pending(q: Optional[asyncio.Queue]) -> list[str]:
    """队列里还没被取走的 game_id。

    直接读内部 deque 而不是 `get_nowait()`：后者也要过 `_get_loop()`，
    队列已经绑在死掉的循环上时它同样会抛 —— 而那正是我们要收拾的场面。
    """
    if q is None:
        return []
    return [x for x in (getattr(q, "_queue", ()) or ()) if isinstance(x, str)]


def _new_queue(items=()) -> asyncio.Queue:
    """给**当前这个事件循环**建一个新队列。

    Py3.10+ 的 `asyncio.Queue` 在首次 `await get()` 时就把自己绑在那个循环上
    （`_LoopBoundMixin`），换个循环再 await 就是 `RuntimeError: ... is bound to a
    different event loop`；而 `put_nowait()` 不碰这个绑定，照样成功 —— 所以复用
    旧队列的 worker 会当场死掉，入队那边却一路“成功”，复盘静默丢失。
    桌面端把后端跑在同进程的一个线程里，重启服务（看门狗自愈、换端口）
    就会换循环复用这些模块级对象 —— 这一条就是那样被发现的。

    队列带上限：无限队列 + 前端可反复点「重新复盘」= 连点 N 次就排 N 遍
    19~45 秒的重算。满了就丢最新请求并记日志（已有一次在跑/在排，何必再来一遍）。
    """
    q = asyncio.Queue(maxsize=_queue_maxsize)
    for gid in items:
        try:
            q.put_nowait(gid)
        except asyncio.QueueFull:
            break
    return q


def _queued_or_running(game_id: str) -> bool:
    """这一局是否已经排着/正在跑（用于 rerun 去重）。"""
    return game_id == _current or game_id in set(_pending(_queue))


def enqueue_review(game_id: str) -> None:
    """把对局加入复盘队列（可从其他线程安全调用）。"""
    global _queue
    loop, q = _worker_loop, _queue
    if loop is None or q is None or loop.is_closed():
        # worker 不在跑（服务还没起来 / 已经停了）。原先这里是往一个没人读的
        # 队列里 put，然后照日志说「已加入复盘队列」—— 那就是静默丢失。
        if game_id not in _orphaned:
            _orphaned.append(game_id)
        logger.warning("复盘：%s 暂时无队可入（worker 未运行），先记下等服务起来", game_id)
        return
    if _queued_or_running(game_id):
        logger.info("复盘：%s 已在队列或正在跑，跳过重复入队", game_id)
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        try:
            q.put_nowait(game_id)
        except asyncio.QueueFull:
            logger.warning("复盘队列已满（%d），丢弃 %s 的入队请求", _queue_maxsize, game_id)
            return
    else:
        # 不在 worker 所在的事件循环里（例如同步接口在线程池执行）→ 跨线程投递
        loop.call_soon_threadsafe(_safe_put, q, game_id)
    logger.info("已加入复盘队列: %s", game_id)


def _safe_put(q: asyncio.Queue, game_id: str) -> None:
    try:
        q.put_nowait(game_id)
    except asyncio.QueueFull:
        logger.warning("复盘队列已满（%d），丢弃 %s", _queue_maxsize, game_id)



async def start_worker() -> None:
    global _queue, _worker_task, _worker_loop
    loop = asyncio.get_running_loop()
    if _queue is None or _worker_loop is not loop:
        # 每个事件循环用自己那只队列（理由见 `_new_queue`）。
        # 上一轮没做完的、以及没 worker 时攒下的，都搬到新队列里接着跑。
        pending = list(dict.fromkeys(_pending(_queue) + _orphaned))
        # 再加上数据库里仍停在 pending 的局：服务被硬杀（或复盘跑到一半关停）时，
        # 那一局的状态会永久停在 pending / 0%，重启后没人重排 —— 前端轮询耗尽后
        # 只能手动点「重新复盘」。
        for gid in _pending_in_db():
            if gid not in pending:
                pending.append(gid)
        _orphaned.clear()
        _queue = _new_queue(pending)
        if pending:
            logger.info("复盘：把上一轮服务留下的 %d 局重新入队", len(pending))
    _worker_loop = loop
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_loop())
        logger.info("复盘 worker 已启动")


def _pending_in_db(min_age_seconds: float = 300.0) -> list[str]:
    """库里「卡住」的待复盘对局 id（供重启后重排）。

    只收 `updated_at` 比现在早 `min_age_seconds` 的行：正常在跑的那一局每 20 手
    就会刷一次 updated_at，不会被误收；而**明显陈旧**的 pending 才是「服务被硬杀
    时留在半路」的那一局。这个年龄门槛同时让测试环境（几秒内建好又跑完的库）
    不会被历史 pending 行干扰。
    """
    try:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            seconds=min_age_seconds)
        with SessionLocal() as db:
            rows = db.scalars(select(GameRecord.id).where(
                GameRecord.review_status == "pending",
                GameRecord.updated_at < cutoff)).all()
            return [str(r) for r in rows]
    except Exception as exc:   # noqa: BLE001
        logger.warning("扫描待复盘对局失败: %s", exc)
        return []


async def stop_worker() -> None:
    global _worker_task, _worker_loop, _queue, _current
    if _worker_task:
        _worker_task.cancel()
        try:
            await _worker_task
        except (asyncio.CancelledError, Exception):   # noqa: BLE001
            pass
        _worker_task = None
    # 队列绑在这个循环上，循环一走它就废了：把没处理的交回 `_orphaned`，
    # 下次启动重建队列时接着跑（而不是留一个“看着有货、永远读不出”的对象）。
    for gid in _pending(_queue):
        if gid not in _orphaned:
            _orphaned.append(gid)
    # 正在跑的那一局同样要交回：否则它永久停在 pending 0%，重启后不再被重排
    if _current and _current not in _orphaned:
        _orphaned.append(_current)
    _current = None
    _queue = None
    _worker_loop = None


async def _loop() -> None:
    global _current
    assert _queue is not None
    while True:
        game_id = await _queue.get()
        _current = game_id
        try:
            await run_review(game_id)
        except asyncio.CancelledError:
            # 关停时被打断：把状态写回 pending，重启后由 start_worker 重排
            _mark_pending(game_id)
            raise
        except Exception as exc:   # noqa: BLE001
            logger.exception("复盘失败 %s: %s", game_id, exc)
            _mark_failed(game_id, str(exc))
        finally:
            _current = None
            _queue.task_done()


def _mark_pending(game_id: str) -> None:
    """把一局退回 pending（取消/中断时用，重启后会被重排）。"""
    try:
        with SessionLocal() as db:
            rec = db.get(GameRecord, game_id)
            if rec is not None and rec.review_status != "done":
                rec.review_status = "pending"
                rec.review_progress = 0.0
                rec.review_stage = STAGE_QUEUED
                rec.review_detail = "排队中（服务重启后会继续）"
                db.commit()
    except Exception as exc:   # noqa: BLE001
        logger.warning("回写 pending 失败 %s: %s", game_id, exc)


def _mark_failed(game_id: str, error: str) -> None:
    with SessionLocal() as db:
        rec = db.get(GameRecord, game_id)
        if rec:
            rec.review_status = "failed"
            rec.review_error = error[:2000]
            rec.review_stage = "failed"
            rec.review_detail = ""
            rec.review_progress = 0.0
            db.commit()


# 阶段与权重：进度条按这些区间映射，让“逐手分析”（耗时大头）占最多长度
STAGE_QUEUED = "queued"        # 0.00 ~ 0.05
STAGE_ENGINE = "engine"        # 0.05 ~ 0.10（等 KataGo 预热）
STAGE_ANALYZE = "analyze"      # 0.10 ~ 0.70（逐手重新分析）
STAGE_REPORT = "report"        # 0.70 ~ 0.75（统计与归类）
STAGE_COMMENT = "comment"      # 0.75 ~ 0.98（讲解生成）
STAGE_DONE = "done"            # 1.00

STAGE_TEXT = {
    STAGE_QUEUED: "排队中",
    STAGE_ENGINE: "等引擎就绪",
    STAGE_ANALYZE: "逐手分析",
    STAGE_REPORT: "统计损失与问题手",
    STAGE_COMMENT: "生成讲解",
    STAGE_DONE: "已完成",
    "failed": "失败",
}


def _set_progress(game_id: str, stage: str, frac: float, detail: str = "") -> None:
    """把进度写回数据库（前端轮询 /status 就能看到走到哪了）。

    复盘一盘棋要几十秒，只给 pending/done 两态时前端看上去就像卡死；
    写入频率很低（每个 chunk / 每批讲解一次），SQLite 上这点开销可以忽略。
    """
    with SessionLocal() as db:
        rec = db.get(GameRecord, game_id)
        if rec is None:
            return
        rec.review_stage = stage
        rec.review_progress = round(max(0.0, min(1.0, frac)), 4)
        rec.review_detail = detail or STAGE_TEXT.get(stage, "")
        db.commit()


def _frac(stage: str, ratio: float = 0.0) -> float:
    """阶段内的完成比例 → 全局进度。"""
    table = {STAGE_QUEUED: (0.0, 0.05), STAGE_ENGINE: (0.05, 0.10),
             STAGE_ANALYZE: (0.10, 0.70), STAGE_REPORT: (0.70, 0.75),
             STAGE_COMMENT: (0.75, 0.98), STAGE_DONE: (1.0, 1.0)}
    lo, hi = table.get(stage, (0.0, 1.0))
    return lo + (hi - lo) * max(0.0, min(1.0, ratio))


# ---------------------------------------------------------------------------
def _rebuild_game(rec: GameRecord) -> Game:
    game = Game(size=rec.size, komi=rec.komi, handicap=rec.handicap,
                score_method=rec.score_method, ko_rule=rec.ko_rule,
                player_color=rec.player_color)
    if game.handicap_stones:
        game.next_color = WHITE
    for m in rec.moves or []:
        point = None if m.get("x") is None else (int(m["x"]), int(m["y"]))
        try:
            game.play(int(m["color"]), point)
        except IllegalMove:
            break
    game.finished = False
    return game


async def analyze_all_turns(game: Game, visits: int,
                            on_progress: Optional[Callable[[int, int], None]] = None) -> list[dict]:
    """逐手重新分析整盘棋，返回 analyses（analyses[i] = 第 i 手后的局面）。"""
    n = len(game.moves)
    turns = list(range(n + 1))
    # 第 i 手之后的行棋方：i<n 时为第 i+1 手的颜色，i=n 时为当前待走方
    side_seq: list[int] = [game.moves[i].color for i in range(n)] + [game.next_color]

    base = AnalysisQuery(
        size=game.size,
        moves=game.gtp_moves(),
        komi=game.komi,
        rules="chinese" if game.score_method == "area" else "japanese",
        initial_stones=game.initial_stones_gtp(),
        initial_player="W" if game.handicap >= 2 else "B",
        max_visits=visits,
        include_ownership=False,
        include_policy=False,
    )
    analyses: list[dict] = []

    async def _run(chunk_turns: list[int], chunk_sides: list[int]) -> list:
        # request_id 必须**不可预测且不复用**：旧实现每局第 0 块都叫 "rev0"，
        # 超时后引擎仍在算的那个块，其迟到响应可能命中下一局的同名 future，
        # 于是 B 局前 20 手用的是 A 局局面。
        q = AnalysisQuery(**{**base.__dict__,
                             "request_id": f"rev{chunk_turns[0]}-{uuid.uuid4().hex[:6]}"})
        return await get_pool().analyze_turns(q, chunk_turns, chunk_sides,
                                              timeout=_chunk_timeout(len(chunk_turns)))

    for start in range(0, len(turns), CHUNK_TURNS):
        chunk_turns = turns[start:start + CHUNK_TURNS]
        chunk_sides = side_seq[start:start + CHUNK_TURNS]
        want_katago = get_pool().katago.available
        results = await _run(chunk_turns, chunk_sides)
        if want_katago and any(r.engine != "katago" for r in results):
            # 整块降级了（多半是这块超过超时）：拆成小块重试一次，
            # 仍失败才接受降级 —— 而不是让 20 手整块掉进启发式。
            logger.warning("复盘第 %d 手起的块降级到启发式，拆成 %d 手小块重试",
                           chunk_turns[0], RETRY_CHUNK_TURNS)
            merged: list = []
            for s in range(0, len(chunk_turns), RETRY_CHUNK_TURNS):
                sub = chunk_turns[s:s + RETRY_CHUNK_TURNS]
                sub_sides = chunk_sides[s:s + RETRY_CHUNK_TURNS]
                merged.extend(await _run(sub, sub_sides))
            if merged:
                results = merged
        for r in results:
            analyses.append(r.to_dict(game.size, top_n=REVIEW_TOP_N, include_ownership=False))
        if on_progress is not None:
            on_progress(min(len(turns), start + CHUNK_TURNS), len(turns))
        await asyncio.sleep(0)      # 让出事件循环，避免长任务卡住请求
    # 对齐（引擎少返回时补空位）
    while len(analyses) < len(turns):
        analyses.append({"turn": len(analyses), "missing": True})
    return analyses[:len(turns)]


async def run_review(game_id: str, force: bool = False) -> dict:
    with SessionLocal() as db:
        rec = db.get(GameRecord, game_id)
        if rec is None:
            raise ValueError(f"对局不存在: {game_id}")
        if rec.review_status == "done" and not force:
            return rec.review_json or {}
        if not rec.moves:
            raise ValueError("空对局无法复盘")
        rec.review_status = "pending"
        rec.review_error = ""
        rec.review_stage = STAGE_QUEUED
        rec.review_progress = 0.0
        rec.review_detail = STAGE_TEXT[STAGE_QUEUED]
        user = db.get(User, rec.user_id)
        user_llm = dict(user.llm_config or {}) if user else {}
        rank_id = rec.rank_id
        player_color = rec.player_color
        ai_name = rec.ai_name
        size = rec.size
        komi = rec.komi
        moves = list(rec.moves)
        result_text = rec.result_text
        db.commit()

    game = _rebuild_game(rec)
    visits = settings.review_visits
    _set_progress(game_id, STAGE_ENGINE, _frac(STAGE_ENGINE),
                  f"等引擎就绪（最多 {int(REVIEW_ENGINE_WAIT)} 秒）…")
    await get_pool().wait_until_ready(timeout=REVIEW_ENGINE_WAIT)

    total_turns = len(game.moves) + 1

    def _on_analyze(done: int, total: int) -> None:
        _set_progress(game_id, STAGE_ANALYZE, _frac(STAGE_ANALYZE, done / max(1, total)),
                      f"逐手分析 {done}/{total} 手")

    _set_progress(game_id, STAGE_ANALYZE, _frac(STAGE_ANALYZE, 0.0),
                  f"逐手分析 0/{total_turns} 手")
    analyses = await analyze_all_turns(game, visits, on_progress=_on_analyze)

    _set_progress(game_id, STAGE_REPORT, _frac(STAGE_REPORT), "统计损失与问题手…")
    reports = analyzer.build_move_reports(moves, analyses, size, player_color)
    ai_color = WHITE if player_color == BLACK else BLACK
    accuracy = analyzer.accuracy_of(reports, player_color)
    ai_accuracy = analyzer.accuracy_of(reports, ai_color)
    counts = analyzer.flag_counts(reports, player_color)
    phases = analyzer.phase_stats(reports, player_color, len(moves), size)
    curve = analyzer.build_curve(analyses)
    moments = analyzer.top_moments(analyses, moves, player_color)
    key = analyzer.key_moves(reports, player_color)

    rank = get_rank(rank_id)
    engine_name = get_pool().active_engine
    # 降级要按**逐手**判定：池级 active_engine 只看 katago.available，覆盖不了
    # 「这一块超时、被启发式填上了合成胜率/目差」的情况（审计 1.6）。每条分析
    # 本来就带真实引擎名（protocol/fallback 都写），以前没人用。
    degraded_plies = [i for i, a in enumerate(analyses)
                      if not a.get("missing")
                      and (a.get("engine") or "katago") != "katago"]
    low_conf = engine_name != "katago" or bool(degraded_plies)
    ctx = {
        "rankName": rank.name,
        "aiName": ai_name,
        # 传给讲解层的引擎口径：只要有任何一手降级，就按「数据不精确」处理
        "engine": "katago" if not low_conf else engine_name,
        "size": size,
        "komi": komi,
        "totalMoves": len(moves),
        "resultText": result_text,
        "playerColorName": color_name(player_color),
    }

    # ---- 讲解 ----
    # 这个标记决定模板敢不敢把胜率与目差当事实说：启发式引擎的两个数不可信，
    # 未装 KataGo（或部分手降级）时只能带限定词出现（见 commentary.template_comment）。
    llm = LLMClient.resolve(user_llm)
    llm_used = False
    llm_error = ""
    comments: dict[str, dict] = {}
    summary: dict = {}
    if llm.configured and key:
        batches = [key[i:i + settings.llm_max_batch]
                   for i in range(0, len(key), settings.llm_max_batch)]
        try:
            for bi, batch in enumerate(batches):
                _set_progress(game_id, STAGE_COMMENT,
                              _frac(STAGE_COMMENT, bi / max(1, len(batches) + 1)),
                              f"大模型讲解关键手 第 {bi + 1}/{len(batches)} 批")
                messages = commentary.build_move_messages(batch, ctx)
                raw = await llm.chat(messages, temperature=0.35, max_tokens=2200)
                parsed = commentary.extract_json(raw) or {}
                for r in batch:
                    item = parsed.get(str(r["moveNum"])) or parsed.get(r["moveNum"])
                    if isinstance(item, dict) and (item.get("reason") or item.get("advice")):
                        comments[str(r["moveNum"])] = {
                            "reason": str(item.get("reason", "")),
                            "advice": str(item.get("advice", "")),
                            "maxim": str(item.get("maxim", "")),
                            "source": "llm",
                        }
                await asyncio.sleep(0)
            _set_progress(game_id, STAGE_COMMENT, _frac(STAGE_COMMENT, 0.9), "生成全局总结…")
            summary_raw = await llm.chat(
                commentary.build_summary_messages(ctx, phases, counts, moments,
                                                  accuracy, ai_accuracy),
                temperature=0.4, max_tokens=1600)
            parsed_summary = commentary.extract_json(summary_raw)
            if parsed_summary and (parsed_summary.get("overall") or parsed_summary.get("opening")):
                summary = {
                    "opening": str(parsed_summary.get("opening", "")),
                    "middle": str(parsed_summary.get("middle", "")),
                    "endgame": str(parsed_summary.get("endgame", "")),
                    "overall": str(parsed_summary.get("overall", "")),
                    "training": [str(x) for x in (parsed_summary.get("training") or [])][:5],
                    "maxim": str(parsed_summary.get("maxim", "")),
                    "source": "llm",
                }
                llm_used = True
            else:
                llm_error = "LLM 返回格式异常，已降级为模板讲解"
        except LLMError as exc:
            llm_error = str(exc)
            logger.warning("LLM 讲解失败，降级模板: %s", exc)
        except Exception as exc:   # noqa: BLE001
            llm_error = f"LLM 异常: {exc}"
            logger.warning(llm_error)
    elif not llm.configured:
        llm_error = "未配置大模型 API Key（设置页可填写），本报告由引擎数据直接生成"

    _set_progress(game_id, STAGE_COMMENT, _frac(STAGE_COMMENT, 0.95),
                  "补齐模板讲解与报告…")

    # 模板补齐：LLM 没覆盖到的关键手 + 总结
    for r in reports:
        if r["flag"] in (analyzer.FLAG_SLOW, analyzer.FLAG_BAD, analyzer.FLAG_BLUNDER) \
                and str(r["moveNum"]) not in comments:
            tpl = commentary.template_comment(r, len(moves), size, low_conf)
            if tpl:
                tpl["source"] = "template"
                comments[str(r["moveNum"])] = tpl
    if not summary:
        summary = commentary.template_summary(ctx, phases, counts, accuracy, ai_accuracy,
                                              moments=moments, reports=reports)
        summary["source"] = "template"

    for r in reports:
        c = comments.get(str(r["moveNum"]))
        if c:
            r["comment"] = c

    review = {
        "version": 1,
        "gameId": game_id,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "engine": engine_name,
        "lowConfidence": low_conf,
        # 哪些手是启发式填的（1-based 手数，便于直接在报告里标注）
        "degradedMoves": [i + 1 for i in degraded_plies],
        "degradedCount": len(degraded_plies),
        "visits": visits,
        "playerColor": player_color,
        "rankName": rank.name,
        "size": size,
        "komi": komi,
        "totalMoves": len(moves),
        "resultText": result_text,
        # 键名带单位语义：值是**目/手**（平均每手损失目数），不是百分比（§3.16）
        "avgLossPoints": accuracy,
        "aiAvgLossPoints": ai_accuracy,
        "counts": counts,
        "phases": phases,
        "keyMoves": [r["moveNum"] for r in key],
        "moments": moments,
        "curve": curve,
        "moves": reports,
        "summary": summary,
        "llm": {"used": llm_used, "model": llm.model if llm_used else "",
                "configured": llm.configured, "error": llm_error},
    }

    with SessionLocal() as db:
        rec = db.get(GameRecord, game_id)
        if rec is not None:
            rec.review_json = review
            rec.review_status = "done"
            rec.review_stage = STAGE_DONE
            rec.review_progress = 1.0
            # 有降级手就把它们写进 detail，前端不必解析整份报告也能提示「部分手精度较低」
            rec.review_detail = (STAGE_TEXT[STAGE_DONE] if not degraded_plies else
                                 f"{STAGE_TEXT[STAGE_DONE]}（{len(degraded_plies)} 手精度较低）")
            rec.llm_used = llm_used
            rec.analyses = analyses           # 复盘用的高精度分析覆盖对局期数据
            if accuracy is not None:
                rec.accuracy = accuracy
            db.commit()

    # 通知仍在对局页的前端
    live = get_hub().get(game_id)
    if live is not None:
        live.emit({"type": "reviewReady", "gameId": game_id,
                   "avgLossPoints": accuracy, "llmUsed": llm_used})
    logger.info("复盘完成 %s：吻合度 %s 目，问题手 %d 处", game_id, accuracy, len(comments))
    return review


def rec_ai_name(game_id: str) -> str:
    """仅用于外部查询（报告导出等）。"""
    with SessionLocal() as db:
        rec = db.get(GameRecord, game_id)
        return rec.ai_name if rec else "AI"


async def review_progress(game_id: str) -> dict:
    with SessionLocal() as db:
        rec = db.get(GameRecord, game_id)
        if rec is None:
            return {"status": "missing"}
        status = rec.review_status
        stage = rec.review_stage or ("done" if status == "done" else "queued")
        # 已完成/失败的局不再给中间进度；旧的 pending 行（没有进度数据）给个底值
        if status == "done":
            progress = 1.0
        elif status == "failed":
            progress = 0.0
        else:
            progress = rec.review_progress or 0.02
        return {
            "status": status,
            "stage": stage,
            "stageText": STAGE_TEXT.get(stage, stage),
            "progress": progress,
            "detail": rec.review_detail or STAGE_TEXT.get(stage, ""),
            "error": rec.review_error,
            "llmUsed": rec.llm_used,
            "avgLossPoints": rec.accuracy,
            "hasReport": bool(rec.review_json),
        }
