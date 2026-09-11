"""审计诊断：直接驱动 KataGo，对比单查询与 analyzeTurns 批量查询的行为。"""
import asyncio
import time

from app.engine.katago import KataGoEngine
from app.engine.protocol import AnalysisQuery
from app.game.rules import BLACK, WHITE, Game


async def main() -> None:
    eng = KataGoEngine(timeout=30.0)
    ok = await eng.start()
    print(f"started={ok} error={eng.error!r}")
    if not ok:
        return

    loop = asyncio.get_running_loop()

    # 1) 单查询（对局路径的形态）
    q = AnalysisQuery(size=19, moves=[], komi=7.5, max_visits=48,
                      include_ownership=True, include_policy=True,
                      human_sl_profile="preaz_18k")
    t = loop.time()
    try:
        r = await eng.analyze(q, side_to_move=BLACK, turn=0)
        print(f"single ok in {loop.time() - t:.2f}s candidates={len(r.candidates)}")
    except Exception as exc:  # noqa: BLE001
        print(f"single FAILED in {loop.time() - t:.2f}s: {exc!r}")

    # 2) analyzeTurns 批量查询（复盘路径的形态）
    g = Game(size=19)
    seq = [(2, 6), (6, 2), (2, 2), (6, 6), (4, 4), (4, 14), (14, 4), (14, 14)]
    for i, (x, y) in enumerate(seq):
        g.play(BLACK if i % 2 == 0 else WHITE, (x, y))
    turns = list(range(len(g.moves) + 1))
    sides = [g.moves[i].color for i in range(len(g.moves))] + [g.next_color]
    q2 = AnalysisQuery(size=19, moves=g.gtp_moves(), komi=g.komi, rules="chinese",
                       max_visits=96, include_ownership=False, include_policy=False)
    t = loop.time()
    try:
        rs = await eng.analyze_turns(q2, turns, sides)
        print(f"batch ok in {loop.time() - t:.2f}s results={len(rs)}")
    except Exception as exc:  # noqa: BLE001
        print(f"batch FAILED in {loop.time() - t:.2f}s: {exc!r}")

    # 3) 复刻 21:16 日志的场景：20 手以上的整盘复盘
    g2 = Game(size=19)
    moves24 = [(2, 6), (6, 2), (2, 2), (6, 6), (4, 4), (4, 14), (14, 4), (14, 14),
               (2, 14), (6, 14), (10, 4), (10, 10), (14, 10), (4, 10), (8, 6),
               (8, 12), (12, 8), (6, 8), (2, 10), (16, 2), (16, 16), (2, 16),
               (10, 16), (16, 8)]
    for i, (x, y) in enumerate(moves24):
        g2.play(BLACK if i % 2 == 0 else WHITE, (x, y))
    turns2 = list(range(len(g2.moves) + 1))
    sides2 = [g2.moves[i].color for i in range(len(g2.moves))] + [g2.next_color]
    q3 = AnalysisQuery(size=19, moves=g2.gtp_moves(), komi=g2.komi, rules="chinese",
                       max_visits=96, include_ownership=False, include_policy=False)
    t = loop.time()
    try:
        rs = await eng.analyze_turns(q3, turns2, sides2)
        print(f"batch25 ok in {loop.time() - t:.2f}s results={len(rs)}")
    except Exception as exc:  # noqa: BLE001
        print(f"batch25 FAILED in {loop.time() - t:.2f}s: {exc!r}")

    await eng.stop()


if __name__ == "__main__":
    asyncio.run(main())
    time.sleep(1)
