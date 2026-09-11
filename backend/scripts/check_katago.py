"""KataGo 引擎体检脚本：不依赖后端服务，直接检查引擎能不能真正应答。

用法：
    python scripts/check_katago.py            # 在 backend/ 下执行

为什么要单独一个体检脚本：`katago version` 通过只代表二进制能跑，
下面这些是各自独立、且都真实踩过的失败点，光看版本号一个都发现不了：
  1. OpenCL/CUDA 后端枚举不到 GPU，或首次运行要做几分钟内核调优；
  2. analysis.cfg 缺必需键 → 启动即退（numAnalysisThreads 等）；
  3. 主网络是 human SL 网络却没写 humanSLProfile → 第一条查询就 FATAL ERROR；
  4. 查询线路格式（moves 必须是配对数组）；
  5. analyzeTurns 每手一行、id 相同，收流逻辑错了就只拿到第一手；
  6. scoreLead / ownership 的视角与行序（错了是静默的数值颠倒，最难发现）；
  7. 档位表里的 humanSLProfile 是否都被这个版本的引擎接受（不接受时不报错，
     而是静默改用默认先验，棋风与档位不符）。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine.katago import KataGoEngine                    # noqa: E402
from app.engine.protocol import AnalysisQuery                 # noqa: E402
from app.rank.defs import RANKS                               # noqa: E402

FAILURES: list[str] = []


def ok(msg: str) -> None:
    print(f"  [OK] {msg}", flush=True)


def bad(msg: str) -> None:
    print(f"  [!!] {msg}", flush=True)
    FAILURES.append(msg)


def info(msg: str) -> None:
    print(f"  [..] {msg}", flush=True)


def step(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def check(cond: bool, good_msg: str, bad_msg: str) -> bool:
    (ok if cond else bad)(good_msg if cond else bad_msg)
    return cond


async def main() -> int:
    eng = KataGoEngine()

    step("一、文件就位")
    if not check(eng.preflight(), f"二进制与权重都在：{Path(eng.bin_path).name}", eng.error):
        info("装引擎：python katago/download.py --mirror https://gh-proxy.com")
        return 1
    info(f"主网络    {Path(eng.model_path).name}")
    info(f"human 网络 {Path(eng.human_model_path).name if eng.human_model_path else '(未启用)'}")
    info(f"配置      {Path(eng.config_path).name}")

    step("二、启动与预热")
    t0 = time.time()
    if not check(await eng.start(), f"引擎就绪，预热耗时 {time.time() - t0:.1f}s",
                 f"启动失败：{eng.error}"):
        info(f"完整引擎输出见：{eng._stderr_path}")
        info("首次运行要做 GPU 内核调优（几分钟），调优结果会缓存，第二次就快了")
        return 1
    if time.time() - t0 > 60:
        info("本次做了 GPU 内核调优，下次启动会快很多")

    step("三、单手分析（9 路，黑走天元后轮白）")
    res = await eng.analyze(AnalysisQuery(size=9, moves=[{"player": "B", "move": "E5"}],
                                          komi=5.5, max_visits=64,
                                          include_ownership=True, include_policy=False),
                            side_to_move=2, turn=1)
    check(res.visits > 0, f"visits={res.visits}，候选 {len(res.candidates)} 个",
          "引擎没给出任何搜索量")
    check(len(res.ownership) == 81, f"ownership {len(res.ownership)} 点（9×9）",
          f"ownership 长度异常：{len(res.ownership)}")
    top = res.candidates[0] if res.candidates else None
    if top:
        info(f"首选 {top.gtp}  visits={top.visits}  胜率(走子方)={top.winrate:.3f}  "
             f"目差(黑视角)={top.score_lead:+.2f}")
        info(f"PV: {' '.join(top.pv[:5])}")

    step("四、视角自洽（黑必胜局面 komi=-30）")
    views = {}
    for label, moves, stm in (("轮黑", [], 1),
                              ("轮白", [{"player": "B", "move": "E5"}], 2)):
        r = await eng.analyze(AnalysisQuery(size=9, moves=moves, komi=-30.0, max_visits=120,
                                            include_ownership=False, include_policy=False),
                              side_to_move=stm, turn=len(moves))
        views[label] = r
        info(f"{label}：目差(黑视角)={r.score_lead:+.2f}  黑胜率={r.winrate_black:.3f}")
    same_sign = views["轮黑"].score_lead > 0 and views["轮白"].score_lead > 0
    check(same_sign, "两种走子方都判为黑领先 —— scoreLead 换算正确",
          "轮黑/轮白符号不一致 —— scoreLead 的视角换算被改坏了，胜率曲线会颠倒")
    check(all(v.winrate_black > 0.9 for v in views.values()),
          "黑胜率都 > 0.9，符合倒贴 30 目的局面", "黑必胜局面却没给出高胜率")

    step("五、批量逐手分析（复盘走的路径）")
    turns = [0, 1, 2, 3]
    got = await eng.analyze_turns(
        AnalysisQuery(size=9, moves=[{"player": "B", "move": "E5"},
                                     {"player": "W", "move": "C3"},
                                     {"player": "B", "move": "G7"}], komi=5.5,
                      max_visits=24, include_ownership=False, include_policy=False),
        turns, [1, 2, 1, 2])
    check(len(got) == len(turns), f"请求 {len(turns)} 手、拿回 {len(got)} 手",
          f"请求 {len(turns)} 手只拿回 {len(got)} 手 —— 复盘会大面积缺数据")
    info(f"逐手目差(黑视角)：{[round(t.score_lead, 2) for t in got]}")

    step("六、human SL 拟人采样（级位档棋风）")
    if eng.human_model_path:
        human = await eng.analyze(AnalysisQuery(size=9, moves=[], komi=5.5, max_visits=24,
                                                include_ownership=False, include_policy=False,
                                                human_sl_profile="preaz_18k"),
                                  side_to_move=1, turn=0)
        check(bool(human.candidates),
              f"18 级档采样成功，首选 {human.candidates[0].gtp}，候选 {len(human.candidates)} 个",
              "human SL 采样没返回候选")
        check(any(c.human_prior for c in human.candidates),
              "候选带 humanPrior —— human SL 网络确实在生效",
              "候选里没有 humanPrior —— human 网络未生效，级位档只剩削弱、没有拟人棋风")

        # 档位表用到的每个档位都要真的被引擎接受。非法值**不会报错到界面上**：
        # analyze() 发现响应带 error 时会静默去掉 human_sl_profile 重试，于是该等级
        # 悄悄改用 analysis.cfg 里的 preaz_9d 先验 —— 棋风与档位不符且无从察觉。
        # 所以这里走 eng.query() 看原始响应，绕开那层重试。换 KataGo 版本后必须重跑。
        used = sorted({p.human_sl_profile for r in RANKS
                       if (p := r.engine) and p.human_sl_profile})
        rejected = []
        for name in used:
            data = await eng.query(AnalysisQuery(size=9, moves=[], komi=5.5, max_visits=8,
                                                 include_ownership=False,
                                                 include_policy=False,
                                                 human_sl_profile=name))
            if isinstance(data, dict) and data.get("error"):
                rejected.append(f"{name}（{str(data['error'])[:60]}）")
        check(not rejected,
              f"档位表用到的 {len(used)} 个 human 档位全部被引擎接受",
              f"这些档位被拒，对应等级会静默改用默认先验：{rejected}")
    else:
        info("没装 human SL 网络，跳过（级位档棋风拟人度会下降，其他功能不受影响）")

    await eng.stop()

    step("结论")
    if FAILURES:
        bad(f"{len(FAILURES)} 项未通过：")
        for f in FAILURES:
            print(f"       - {f}", flush=True)
        info(f"引擎原始输出：{eng._stderr_path}")
        return 1
    ok("KataGo 全链路可用，后端启动后会自动使用它（复盘报告会标为高精度）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
