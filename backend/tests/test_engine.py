"""引擎层测试：启发式降级引擎、温度采样选点、批量逐手分析、随机整局自洽性。"""
from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path

from app.engine.fallback import HeuristicEngine
from app.engine.pool import EnginePool
from app.engine.protocol import AnalysisQuery, AnalysisResult, Candidate
from app.game.rules import BLACK, EMPTY, WHITE, Board, Game, to_gtp
from app.rank.defs import get_engine_profile


def make_query(game: Game, visits: int = 8) -> AnalysisQuery:
    return AnalysisQuery(size=game.size, moves=game.gtp_moves(), komi=game.komi,
                         initial_stones=game.initial_stones_gtp(),
                         max_visits=visits, include_ownership=True, include_policy=False)


def stones_query(size: int, black_pts, white_pts, komi: float = 5.5,
                 visits: int = 8) -> AnalysisQuery:
    """把摆好的子以 initialStones 形式交给引擎（测试用，也覆盖让子查询路径）。"""
    stones = [["B", to_gtp(p, size)] for p in black_pts] + \
             [["W", to_gtp(p, size)] for p in white_pts]
    return AnalysisQuery(size=size, moves=[], komi=komi, initial_stones=stones,
                         initial_player="B", max_visits=visits,
                         include_ownership=True, include_policy=False)


# ---------------------------------------------------------------------------
# 启发式引擎
# ---------------------------------------------------------------------------
def test_heuristic_returns_legal_candidates():
    g = Game(size=9, komi=5.5)
    g.play(BLACK, (2, 6))
    g.play(WHITE, (6, 2))
    res = HeuristicEngine(seed=7).analyze(make_query(g), side_to_move=BLACK, turn=2)

    assert res.candidates, "必须给出候选点"
    assert 0.0 <= res.winrate <= 1.0
    assert len(res.ownership) == 81
    assert all(-1.0 <= v <= 1.0 for v in res.ownership)
    for c in res.candidates:
        assert c.point is None or g.board.at(c.point) == EMPTY
    # 布局阶段偏好三、四线而不是天元附近的一路
    best = res.candidates[0]
    assert best.point is not None
    x, y = best.point
    assert 1 <= min(x, y, 8 - x, 8 - y) <= 4


def test_heuristic_captures_hanging_stone():
    """对方单子只剩一口气时应优先提子。"""
    q = stones_query(9, [(3, 4), (5, 4), (4, 5)], [(4, 4)])
    res = HeuristicEngine(seed=1).analyze(q, side_to_move=BLACK, turn=0, noise=0.0)
    assert res.candidates[0].point == (4, 3), "应提掉只有一口气的白子"


def test_heuristic_avoids_filling_own_eye():
    """已活棋的单眼不应被当作候选点。"""
    black = [(3, 4), (5, 4), (4, 3), (4, 5)]
    q = stones_query(9, black, [(0, 0)])
    res = HeuristicEngine(seed=2).analyze(q, side_to_move=BLACK, turn=0, noise=0.0)
    points = [c.point for c in res.candidates]
    assert (4, 4) not in points, "不应建议填自己的眼"


def test_heuristic_ownership_sign_convention():
    """黑方地盘在「黑视角」输出中应为正值（KataGo ownership 为白方正）。"""
    q = stones_query(9, [(4, y) for y in range(9)], [(5, y) for y in range(9)])
    res = HeuristicEngine(seed=3).analyze(q, side_to_move=BLACK, turn=0, noise=0.0)
    black_view = res.ownership_black_view(9)
    assert len(black_view) == 81
    # (0,0) 在左侧黑阵中 → 黑视角为正；(8,8) 在右侧白阵中 → 负
    assert black_view[0 * 9 + 0] > 0.5
    assert black_view[8 * 9 + 8] < -0.5


def test_heuristic_batch_turns():
    g = Game(size=9, komi=5.5)
    g.play(BLACK, (2, 6))
    g.play(WHITE, (6, 2))
    g.play(BLACK, (2, 2))
    q = make_query(g)
    turns = [0, 1, 2, 3]
    sides = [BLACK, WHITE, BLACK, WHITE]
    results = HeuristicEngine(seed=5).analyze_turns(q, turns, sides)
    assert len(results) == 4
    assert [r.turn for r in results] == turns
    assert [r.side_to_move for r in results] == sides
    assert all(r.candidates for r in results)


# ---------------------------------------------------------------------------
# 选点策略
# ---------------------------------------------------------------------------
def _fake_result(visits: list[int]) -> AnalysisResult:
    """构造伪分析结果；候选点均在 9 路棋盘内。"""
    cands = [Candidate(point=(i % 9, i // 9), gtp=to_gtp((i % 9, i // 9), 9), visits=v,
                       winrate=0.5, score_lead=0.0, rank=i)
             for i, v in enumerate(visits)]
    return AnalysisResult(turn=0, side_to_move=BLACK, candidates=cands,
                          winrate=0.5, score_lead=0.0, engine="fake")


def test_choose_move_zero_tolerance_picks_best():
    board = Board(9)
    profile = get_engine_profile(27)          # 九段：容忍度 0
    pool = EnginePool(seed=11)
    picks = {pool.choose_move(board, BLACK, _fake_result([100, 60, 30, 10]), profile)
             for _ in range(20)}
    assert picks == {(0, 0)}, "满配档位必须稳定选择最佳点"


def test_choose_move_wide_tolerance_explores():
    board = Board(9)
    profile = get_engine_profile(1)           # 18级：大容忍度 + 宽 top_n
    pool = EnginePool(seed=13)
    picks = {pool.choose_move(board, BLACK, _fake_result([50] * 12), profile)
             for _ in range(300)}
    assert len(picks) >= 5, "弱档位应体现出多样性（更像人类）"


def test_choose_move_blunder_picks_worse_candidate():
    """手滑必须从 top_n 之外挑，否则它跟正常采样没区别。

    旧版用 10 个候选 + 18级画像测，但 18级 的 top_n 是 16 —— `len(playable) > top_n`
    永远不成立，手滑分支根本不会执行，测试是绿的但什么也没盯住。
    """
    board = Board(9)
    pool = EnginePool(seed=17)
    result = _fake_result([100 - i for i in range(24)])
    # 九段本来只下最优点；把手滑拉满，就应当稳定落到 top_n 之外
    prof = replace(get_engine_profile(27), blunder_rate=1.0, sample_top_n=4)
    picks = [pool.choose_move(board, BLACK, result, prof) for _ in range(200)]
    indices = {(p[1] * 9 + p[0]) for p in picks if p is not None}
    assert indices and min(indices) >= 4, \
        f"手滑必须挑 top_n 之外的候选，实际最小下标 {min(indices) if indices else None}"


def test_choose_move_local_noise_leaves_engine_candidates():
    """local_noise 生效时必须离开引擎候选，去下紧贴棋子的点。

    这是级位档唯一的实质削弱手段：KataGo 报出的候选全是它认可的好点，
    在候选里怎么采样每手损失都封顶在 ~3 目（实测 36 种参数组合最高 1.66 目）。
    """
    board = Board(9)
    board.place(BLACK, [(4, 4)])
    board.place(WHITE, [(1, 7)])
    pool = EnginePool(seed=31)
    # 引擎只认两个远处的角，紧贴棋子的邻点一个都不在候选里
    result = AnalysisResult(turn=2, side_to_move=BLACK, candidates=[
        Candidate(point=(0, 0), gtp="A1", visits=100, rank=0),
        Candidate(point=(8, 0), gtp="J1", visits=80, rank=1),
    ], winrate=0.55, score_lead=1.0, engine="fake")

    noisy = replace(get_engine_profile(1), local_noise=1.0)      # 必然走噪声
    picks = {pool.choose_move(board, BLACK, result, noisy) for _ in range(80)}
    assert (0, 0) not in picks and (8, 0) not in picks, "噪声档位不该再走引擎候选"
    stones = [(4, 4), (1, 7)]
    assert all(any(max(abs(x - sx), abs(y - sy)) <= 1 for sx, sy in stones)
               for x, y in picks), "噪声点必须紧贴已有棋子（含斜邻）"

    quiet = replace(get_engine_profile(1), local_noise=0.0)
    picks2 = {pool.choose_move(board, BLACK, result, quiet) for _ in range(40)}
    assert picks2 <= {(0, 0), (8, 0)}, "关掉噪声就该只在候选里选"


def test_local_points_skips_eyes_and_occupied():
    """噪声点不能是自杀手或自己的眼，否则新手 AI 会自己把自己填死。"""
    board = Board(9)
    board.place(BLACK, [(3, 4), (5, 4), (4, 3), (4, 5)])   # (4,4) 是黑的眼
    pts = EnginePool._local_points(board, BLACK)
    assert (4, 4) not in pts, "不应把自己的眼当成噪声点"
    assert all(board.at(p) == EMPTY for p in pts)


def test_tolerance_weights_are_denominated_in_points():
    """采样权重必须以「亏多少目」为准，且容忍度越大越平均。

    这是修掉「七段比五段弱」的关键：旧实现按 humanPrior 采样，而 humanPrior
    的 argmax 与引擎最优点不是同一个点，于是高段反而更容易选到差点；
    再旧一点按 visits 采样，但 visits 在高搜索量下极度尖峰，幂运算几乎不改变
    分布，段位档因此削不动（实测三/五段都只剩 0.4 目 = 职业量级）。
    """
    cands = [Candidate(point=(i, 0), gtp=to_gtp((i, 0), 9), visits=v,
                       score_lead=sl, rank=i)
             for i, (v, sl) in enumerate(((900, 2.0), (200, 1.0), (60, -1.0), (40, -3.0)))]
    # 内部约定 score_lead 是黑方视角；黑先走时它就等于走子方视角，最优点 = 下标 0
    tight = EnginePool._tolerance_weights(cands, BLACK, 0.2)
    loose = EnginePool._tolerance_weights(cands, BLACK, 4.0)
    assert tight[0] > 0.99, "容忍度极小时必须几乎只选最优点"
    assert tight[1] < 0.01, "亏 1 目在 0.2 目容忍度下几乎不可能被选中"
    assert loose[0] < tight[0], "容忍度放大后最优点占比应下降"
    assert loose[3] > tight[3], "容忍度放大后差点被选中的概率应上升"
    # visits 极度尖峰（900 vs 40）也不能影响权重，否则又退回旧坑
    flat = [replace(c, visits=1) for c in cands]
    assert EnginePool._tolerance_weights(flat, BLACK, 0.2) == tight
    # 白先走时 score_lead 是黑视角，翻号后最优点应是原来最差的那个
    assert EnginePool._tolerance_weights(cands, WHITE, 0.2)[3] > 0.99


def test_choose_move_rejects_illegal_and_eye_points():
    board = Board(9)
    board.place(BLACK, [(3, 4), (5, 4), (4, 3), (4, 5)])   # (4,4) 是黑的眼
    pool = EnginePool(seed=19)
    result = AnalysisResult(turn=0, side_to_move=BLACK, candidates=[
        Candidate(point=(4, 4), gtp="E5", visits=100, rank=0),
        Candidate(point=(0, 0), gtp="A1", visits=50, rank=1),
    ], winrate=0.6, score_lead=1.0, engine="fake")
    pick = pool.choose_move(board, BLACK, result, get_engine_profile(27))
    assert pick == (0, 0), "不应填自己的眼"


def test_is_playable_filters_occupied():
    board = Board(9)
    board.place(WHITE, [(2, 2)])
    pool = EnginePool()
    assert not pool._is_playable(board, BLACK, (2, 2))
    assert pool._is_playable(board, BLACK, (6, 6))
    assert pool._is_playable(board, BLACK, None)


# ---------------------------------------------------------------------------
# 引擎池（KataGo 缺席时自动降级）
# ---------------------------------------------------------------------------
def test_pool_falls_back_when_katago_missing():
    async def run():
        pool = EnginePool(seed=23)
        await pool.startup()
        assert pool.active_engine == "heuristic"
        g = Game(size=9, komi=5.5)
        res = await pool.analyze(make_query(g), side_to_move=BLACK, turn=0,
                                 profile=get_engine_profile(1))
        assert res.engine == "heuristic"
        assert res.candidates
        await pool.shutdown()
        return res

    res = asyncio.run(run())
    assert res.to_dict(9)["candidates"], "分析结果需可序列化为前端结构"


def test_pool_survives_a_second_event_loop():
    """同进程里第二次起后端不能炸：asyncio 原语会把首个事件循环绑死。

    桌面端把后端跑在同一进程的一个线程里，而 `EnginePool` 是模块级单例；重启服务
    （看门狗自愈、换端口）会新建一个事件循环复用同一个对象。Py3.10+ 的
    Lock/Event 在**首次 await 时**就记下自己的 loop，第二次从新循环 await 就是
    `RuntimeError: ... is bound to a different event loop`。

    这个坑只有真装了 KataGo 才会暴露：看门狗任务是在 preflight 成功之后才创建的，
    没装引擎时根本没人 await 那个 Event —— 所以本测试在 `GO_KATAGO_ENABLED=false`
    下也成立，它直接 await 那些原语来代替看门狗。
    """
    async def boot(p):
        await p.startup()
        async with p._boot_lock:            # 看门狗之外的另一个原语，一起验
            pass
        try:
            await asyncio.wait_for(p.katago.dead_event.wait(), timeout=0.01)
        except asyncio.TimeoutError:
            pass                            # 期望就是超时；要的是它不抛 RuntimeError
        await p.shutdown()

    p = EnginePool(seed=41)
    for _ in range(2):                      # 两个各自独立、用完即关的事件循环
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(boot(p))
        finally:
            loop.close()


def test_pool_random_full_game_no_illegal_moves():
    """用弱档位画像跑完整局 9 路棋，验证选点始终合法、流程自洽。"""
    async def run():
        pool = EnginePool(seed=29)
        await pool.startup()
        g = Game(size=9, komi=5.5)
        profile = get_engine_profile(5)
        plies = 0
        while not g.finished and plies < 160:
            res = await pool.analyze(make_query(g, visits=6), side_to_move=g.next_color,
                                     turn=len(g.moves), profile=profile)
            point = pool.choose_move(g.board, g.next_color, res, profile)
            g.play(g.next_color, point)
            plies += 1
        await pool.shutdown()
        return g, plies

    g, plies = asyncio.run(run())
    assert plies > 10, "对局应能正常推进"
    stones = sum(1 for row in g.board.grid for v in row if v != EMPTY)
    assert stones > 0


def test_katago_preflight_reports_missing_binary():
    """未安装 KataGo 时应给出可操作的提示，而不是抛异常。"""
    from app.config import settings as app_settings
    from app.engine.katago import KataGoEngine

    original = app_settings.katago_enabled
    app_settings.katago_enabled = True
    try:
        eng = KataGoEngine(bin_path="./definitely-missing-katago.exe",
                           model_path="./missing-model.bin.gz")
        assert eng.preflight() is False
        assert "未找到" in eng.error
        assert eng.status()["available"] is False
    finally:
        app_settings.katago_enabled = original


# ---------------------------------------------------------------------------
# 断链自愈：KataGo 运行期意外退出后必须能自己回来
# （旧行为：available 一旦置 False 就再也不回 True，只能重启服务）
# ---------------------------------------------------------------------------
class _EofStream:
    async def read(self, n: int = -1) -> bytes:
        return b""


class _RaisingStream:
    """管道被强拆：不是 EOF，而是 read() 直接抛异常（另一种死法）。"""

    async def read(self, n: int = -1) -> bytes:
        raise ConnectionResetError("管道被强拆")


class _StubProc:
    def __init__(self, stream):
        self.stdout = stream
        self.stdin = None
        self.returncode = None


def _drive_read_loop(eng, stream, stderr_text: str | None = None) -> str:
    """跑一次读循环到流结束，返回它交给在飞查询的错误（没交出就返回空串）。

    默认把 stderr 路径指向不存在的文件，否则 `_stderr_tail()` 会读到本机真实引擎日志，
    断言就会跟着磁盘内容飘。传 `stderr_text` 就是反过来说「磁盘上确实有东西」，
    拿它验「EOF 那一支不许把 stderr 当死因」（见下面那条）。
    """
    if stderr_text is None:
        eng._stderr_path = eng._stderr_path.with_name("no-such-test-stderr.log")
    else:
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False,
                                         encoding="utf-8") as f:
            f.write(stderr_text)
        eng._stderr_path = Path(f.name)      # 不用 mkstemp：它给的 fd 没人关

    async def run():
        eng.proc = _StubProc(stream)
        fut = asyncio.get_running_loop().create_future()
        eng._pending["q1"] = fut
        task = asyncio.create_task(eng._read_loop())
        try:
            try:
                await asyncio.wait_for(fut, timeout=3)
                return ""
            except RuntimeError as exc:
                return str(exc)
            except (TimeoutError, asyncio.TimeoutError):
                return ""      # 只能干等超时 = 缺陷，交给下面的断言判红
            finally:
                await asyncio.wait_for(asyncio.shield(task), timeout=3)
        finally:
            if not task.done():
                task.cancel()

    return asyncio.run(run())


def test_unexpected_eof_counts_death_and_wakes_watchdog():
    """引擎意外退出：降级 + 计一次断链 + 摇醒看门狗 + 在飞查询不等超时。"""
    from app.engine.katago import KataGoEngine

    eng = KataGoEngine()
    eng.available = True
    reason = _drive_read_loop(eng, _EofStream())
    assert reason, "挂着 Future 的调用方必须立刻拿到错误，不能干等 60s"
    assert "输出流结束" in reason
    assert eng.available is False
    assert eng.death_count == 1
    assert eng.dead_event.is_set(), "看门狗就是靠这个信号被唤醒的"
    assert eng.last_death_at


#: 引擎正常开机就会写进 stderr 的那几行（本机真杀一次 katago.exe 拷下来的现场）
STARTUP_BANNER = (
    "2026-09-08 12:10:18+0800: Loaded config D:\\AI\\围棋\\backend\\katago\\analysis.cfg\n"
    "2026-09-08 12:10:18+0800: Loaded model kata_b18c384nbt-humanv0.bin.gz\n"
    "2026-09-08 12:10:18+0800: Analyzing up to 2 positions at a time in parallel\n"
    "2026-09-08 12:10:18+0800: Started, ready to begin handling requests\n")


def test_eof_reason_is_not_the_startup_banner():
    """运行期被外部结束时，“死因”不许是引擎的开机话。

    现场：P5 验收真杀 katago.exe，设置页那行成了「运行中断链 1 次：Loaded config … |
    Loaded model … | Started, ready to begin handling requests」—— 学员读到的是
    「引擎看起来挺好」，而它其实刚被杀。根因是那一支写成
    `reason = self.error or self._stderr_tail() or …`：致命错误走 stdout（已进
    self.error），运行期的 stderr 里通常只剩开机的几行，所以这个兜底几乎永远命中噪声。

    上面那条测试照不到它：那个夹具专门把 stderr 指向不存在的文件（为了不把断言
    绑在磁盘内容上），于是 `_stderr_tail()` 恒为空。**桩躲过了真链路能抓到的缺陷**。
    """
    from app.engine.katago import KataGoEngine

    eng = KataGoEngine()
    eng.available = True
    reason = _drive_read_loop(eng, _EofStream(), stderr_text=STARTUP_BANNER)
    assert "Loaded config" not in reason and "ready to begin" not in reason, reason
    assert "输出流结束" in reason, reason
    assert eng.status()["lastDeathReason"] == reason

    # 有真死因时优先级不变：stdout 上的 FATAL 文本仍要盖住一切兜底说法
    eng2 = KataGoEngine()
    eng2.available = True
    eng2.error = "FATAL ERROR: SGFMetadata is required for the model"
    got = _drive_read_loop(eng2, _EofStream(), stderr_text=STARTUP_BANNER)
    assert got == eng2.error or eng2.error in got, got


def test_broken_pipe_also_counts_as_death():
    """走的是另一条分支：read() 抛异常（不是 EOF），同样要算断链并立刻交错。"""
    from app.engine.katago import KataGoEngine

    eng = KataGoEngine()
    eng.available = True
    reason = _drive_read_loop(eng, _RaisingStream())
    assert "读取失败" in reason, "读循环报错也要给在飞查询一个错误，不能等超时"
    assert eng.death_count == 1
    assert eng.dead_event.is_set()


def test_graceful_stop_eof_is_not_counted_as_death():
    """主动关停与引擎崩盘在 EOF 上长得一样，不分就每次重启都虚增一条断链。"""
    from app.engine.katago import KataGoEngine

    eng = KataGoEngine()
    eng.available = True
    eng._stopping = True            # stop() 会置的这个位
    _drive_read_loop(eng, _EofStream())
    assert eng.death_count == 0
    assert not eng.dead_event.is_set(), "关停不该触发自动重启"
    assert eng.available is False


def test_death_reason_survives_recovery():
    """恢复后 error 会清空，但死因要留下来能查到上次到底为什么死。"""
    from app.engine.katago import KataGoEngine

    eng = KataGoEngine()
    eng._mark_dead("FATAL ERROR: SGFMetadata is required")
    eng.available = True
    eng.error = ""                  # 与 start() 成功路径一致
    st = eng.status()
    assert st["error"] == ""
    assert st["deathCount"] == 1
    assert "SGFMetadata" in st["lastDeathReason"]


class _StubKataGo:
    """只实现看门狗用到的那几个成员，避免测试真拉起 katago.exe。"""

    def __init__(self, fail_times: int):
        self.dead_event = asyncio.Event()
        self.available = False
        self.warming = False
        self.error = ""
        self.last_death_reason = "KataGo 输出流结束"
        self.stderr_log_path = "katago.stderr.log"
        self.start_calls = 0
        self.stop_calls = 0
        self._fail_times = fail_times

    async def start(self) -> bool:
        self.start_calls += 1
        if self.start_calls <= self._fail_times:
            self.error = f"预热失败（stub 第 {self.start_calls} 次）"
            return False
        self.available = True
        self.error = ""
        return True

    async def stop(self) -> None:
        self.stop_calls += 1
        self.available = False

    def status(self) -> dict:
        return {"name": "katago", "available": self.available, "warming": self.warming,
                "deathCount": 1 if self.start_calls else 0}


async def _wait_until(cond, timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


def _run_watchdog(fail_times: int):
    """把看门狗跑一轮，返回（stub, pool, 监控是否还在守）。

    watching 必须在取消任务之前取：取消之后再问永远是 False，什么也测不出来。
    """
    from app.engine import pool as pool_mod

    async def run():
        original = pool_mod.RESTART_SCHEDULE
        # 不缩短退避的话，这个测试要真等 5/15/30/60/60 秒
        pool_mod.RESTART_SCHEDULE = (0.01,) * len(original)
        pool = EnginePool(seed=5)
        stub = _StubKataGo(fail_times)
        pool.katago = stub
        pool.started = True
        task = asyncio.create_task(pool._supervise())
        pool._supervise_task = task
        stub.dead_event.set()          # 模拟读循环报了一次断链
        try:
            await _wait_until(lambda: stub.available or task.done(), timeout=3)
            return stub, pool, pool.status()["recover"]["watching"], task
        finally:
            pool_mod.RESTART_SCHEDULE = original

    stub, pool, watching, task = asyncio.run(run())
    pool.started = False
    if not task.done():
        task.cancel()
    return stub, pool, watching


def test_watchdog_restarts_engine_after_backoff():
    """断链后要自动重启，第一次失败就再试下一次（退避序列内）。"""
    stub, pool, watching = _run_watchdog(fail_times=1)
    assert stub.available is True, "看门狗必须把引擎拉回来，而不是永久降级"
    assert stub.start_calls == 2
    assert pool.restart_count == 0, "已恢复就不该再占着重启额度"
    assert pool.active_engine == "katago"
    assert watching, "自愈一次就收工的话，第二次断链又没人管了"


def test_watchdog_gives_up_instead_of_restart_storm():
    """死因稳定复现时必须封顶：无限重启会把机器拖垮。"""
    from app.engine.pool import MAX_RESTARTS

    stub, pool, watching = _run_watchdog(fail_times=99)
    assert stub.start_calls == MAX_RESTARTS, "应恰好试完退避序列那么多次数，不多不少"
    assert stub.available is False
    assert pool.active_engine == "heuristic", "放弃自愈后照常用启发式引擎下棋"
    assert watching is False, "放弃自愈后监控要自己退出，不能还在等下一次断链"


# ---------------------------------------------------------------------------
# 估值口径：静态影响力估值天然偏爱子多的一方，必须先手修正
# ---------------------------------------------------------------------------
def test_heuristic_root_is_parity_neutral():
    """黑下一子后轮到白走，root 不应因「黑多一子」而凭空涨好几目。

    修正前：base_eval 每多一子就涨 ~5.4 目，胜率曲线逐手锯齿。
    """
    eng = HeuristicEngine(seed=3)
    g = Game(size=9, komi=5.5)
    before = eng.analyze(make_query(g), side_to_move=BLACK, turn=0, noise=0.0)
    assert before.candidates[0].point is not None
    g.play(BLACK, before.candidates[0].point)
    after = eng.analyze(make_query(g), side_to_move=WHITE, turn=1, noise=0.0)
    assert abs(after.score_lead - before.score_lead) < 1.5


def test_heuristic_score_curve_stays_smooth_over_a_game():
    """20 手对局的目差曲线：摆动小、无系统性漂移。

    必须用**满配画像**落子（关掉 local_noise / blunder / tolerance）：这个测试验的是
    估值的奇偶修正，而 local_noise 会把子下到候选之外 —— 双方一旦下出无理手，
    局面就真的倾斜了，那是合法的估值变化而不是缺陷，只会给断言灌进采样方差
    （实测用 14级 画像时 21 个样本的均值偏差 3.22 目，纯粹是方差顶穿了 3.0 阈值）。
    """
    eng = HeuristicEngine(seed=11)
    pool = EnginePool(seed=11)
    profile = replace(get_engine_profile(5), local_noise=0.0, blunder_rate=0.0,
                      sample_tolerance=0.0, sample_top_n=1)
    g = Game(size=9, komi=5.5)
    roots = [eng.analyze(make_query(g), side_to_move=g.next_color, turn=0,
                         noise=0.0).score_lead]
    for _ in range(20):
        res = eng.analyze(make_query(g), side_to_move=g.next_color,
                          turn=len(g.moves), noise=0.0)
        point = pool.choose_move(g.board, g.next_color, res, profile)
        g.play(g.next_color, point)
        roots.append(eng.analyze(make_query(g), side_to_move=g.next_color,
                                 turn=len(g.moves), noise=0.0).score_lead)

    swings = [abs(roots[i + 1] - roots[i]) for i in range(len(roots) - 1)]
    avg = sum(swings) / len(swings)
    assert avg < 1.5, f"曲线仍有锯齿：平均逐手摆动 {avg:.2f} 目"
    # 双方都按启发式的最优应对弈，20 手后估值不应偏离空盘基准（-komi）太远
    assert abs(sum(roots) / len(roots) + g.komi) < 3.0


def test_best_candidate_score_matches_root_scale():
    """最佳候选的 scoreLead 应与 rootInfo.scoreLead 同口径（KataGo 语义）。

    注意要用行棋方与实际子数奇偶一致的局面（黑先走时子数持平，白先走时黑多一子），
    否则先手修正量的前提不成立。
    """
    eng = HeuristicEngine(seed=13)
    g = Game(size=9, komi=5.5)
    checked = 0
    for ply in range(6):
        res = eng.analyze(make_query(g), side_to_move=g.next_color, turn=ply, noise=0.0)
        best = res.candidates[0]
        if best.point is None:
            break
        # 首选不会差于 root，但也不应高出一个先手价值（修正前会高 ~5 目）
        assert best.score_lead >= res.score_lead - 0.5
        assert best.score_lead - res.score_lead < 3.0
        checked += 1
        g.play(g.next_color, best.point)
    assert checked >= 4


def test_eval_after_move_counts_captures_for_both_colors():
    """提子对双方的估值影响应完全对称（白的提子曾被漏算，且影响力增量符号写反）。"""
    eng = HeuristicEngine(seed=1)
    around = [(3, 4), (5, 4), (4, 5)]

    board = Board(9)
    board.place(BLACK, around)
    board.place(WHITE, [(4, 4)])
    black_gains = eng._eval_after_move(board, eng._influence_field(board),   # noqa: SLF001
                                      0.0, BLACK, (4, 3))

    mirrored = Board(9)
    mirrored.place(WHITE, around)
    mirrored.place(BLACK, [(4, 4)])
    white_gains = eng._eval_after_move(mirrored, eng._influence_field(mirrored),  # noqa: SLF001
                                       0.0, WHITE, (4, 3))

    assert black_gains is not None and white_gains is not None
    assert black_gains > 0 > white_gains, "提子应让提子方得利"
    assert abs(black_gains + white_gains) < 0.01, "黑白镜像局面的估值必须互为相反数"


def test_eval_after_move_rejects_suicide():
    """自杀手不参与估值（root 需要对双方试下，同一点对两方合法性不同）。"""
    eng = HeuristicEngine(seed=1)
    board = Board(9)
    board.place(BLACK, [(1, 0), (0, 1)])
    value = eng._eval_after_move(board, eng._influence_field(board),   # noqa: SLF001
                                0.0, WHITE, (0, 0))
    assert value is None
    assert board.at((0, 0)) == EMPTY, "试下后必须还原棋盘"


def test_analyze_turns_forces_actual_move_into_candidates():
    """复盘时「当时实际下出的一手」必须出现在候选里，否则算不出同节点损失。"""
    eng = HeuristicEngine(seed=5)
    g = Game(size=9, komi=5.5)
    g.play(BLACK, (0, 0))       # 1-1 角：启发式排序绝不会把它放进前 12
    g.play(WHITE, (4, 4))
    out = eng.analyze_turns(make_query(g), [0, 1, 2], [BLACK, WHITE, BLACK])

    assert len(out) == 3
    assert (0, 0) in [c.point for c in out[0].candidates], "黑首手 1-1 应被强制纳入"
    assert (4, 4) in [c.point for c in out[1].candidates], "白首手天元应被强制纳入"
    # 被纳入的差手应被评为明显亏（一线/角上 1-1 在 9 路盘是恶手）
    best = max(c.score_lead for c in out[0].candidates if c.point is not None)
    played = next(c for c in out[0].candidates if c.point == (0, 0))
    assert best - played.score_lead > 2.0
