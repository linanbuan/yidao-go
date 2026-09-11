"""复盘报告导出（Markdown，可直接在浏览器打印为 PDF）。"""
from __future__ import annotations

from .analyzer import FLAG_LABEL, PHASE_LABEL


def review_to_markdown(review: dict, sgf: str = "") -> str:
    if not review:
        return "# 复盘报告\n\n（暂无数据）\n"
    summary = review.get("summary") or {}
    counts = review.get("counts") or {}
    phases = review.get("phases") or {}
    llm = review.get("llm") or {}
    lines: list[str] = []
    ap = lines.append

    ap(f"# 围棋复盘报告 · {review.get('rankName', '')}")
    ap("")
    ap(f"- 对局编号：`{review.get('gameId', '')}`")
    ap(f"- 生成时间：{review.get('generatedAt', '')}")
    ap(f"- 棋盘：{review.get('size', 19)} 路　贴目：{review.get('komi', 7.5)}　"
       f"共 {review.get('totalMoves', 0)} 手")
    ap(f"- 玩家执{('黑' if review.get('playerColor') == 1 else '白')}")
    ap(f"- 结果：{review.get('resultText', '')}")
    ap(f"- 分析引擎：{review.get('engine', '')}（每手 {review.get('visits', 0)} visits）")
    # 兼容旧报告：改名前的键是 accuracy / aiAccuracy（§3.16），已落库的仍在用
    acc = review.get("avgLossPoints", review.get("accuracy"))
    ai_acc = review.get("aiAvgLossPoints", review.get("aiAccuracy"))
    ap(f"- 吻合度（平均每手损失目数）：玩家 **{acc if acc is not None else '—'}** 目　"
       f"AI {ai_acc if ai_acc is not None else '—'} 目")
    ap(f"- 问题手统计：大恶手 {counts.get('blunder', 0)} · 恶手 {counts.get('bad', 0)} · "
       f"缓手 {counts.get('slow', 0)} · 好手 {counts.get('good', 0)}")
    ap("")

    ap("## 一、整局总结")
    ap("")
    for key in ("opening", "middle", "endgame"):
        text = summary.get(key)
        if text:
            ap(f"**{PHASE_LABEL[key]}**：{text}")
            ap("")
    if summary.get("overall"):
        ap(f"**总评**：{summary['overall']}")
        ap("")
    if summary.get("training"):
        ap("**训练建议**：")
        for t in summary["training"]:
            ap(f"- {t}")
        ap("")
    if summary.get("maxim"):
        ap(f"> {summary['maxim']}")
        ap("")

    ap("## 二、分阶段数据")
    ap("")
    ap("| 阶段 | 手数 | 平均每手损失（目） | 累计损失（目） | 最大失误 |")
    ap("| --- | --- | --- | --- | --- |")
    for key, label in PHASE_LABEL.items():
        p = phases.get(key) or {}
        avg = p.get("avgLoss")
        worst = f"第 {p.get('worstMoveNum')} 手（{p.get('worstLoss')} 目）" \
            if p.get("worstMoveNum") else "—"
        ap(f"| {label} | {p.get('moves', 0)} | {avg if avg is not None else '—'} | "
           f"{p.get('totalLoss', 0)} | {worst} |")
    ap("")

    moments = review.get("moments") or []
    if moments:
        ap("## 三、全局转折点")
        ap("")
        for m in moments:
            who = "玩家" if m.get("isPlayer") else "AI"
            color = "黑" if m.get("color") == 1 else "白"
            direction = "↑" if m.get("swing", 0) > 0 else "↓"
            ap(f"- 第 {m.get('moveNum')} 手（{who}执{color} {m.get('gtp')}）："
               f"黑方胜率{direction}{abs(m.get('swing', 0)) * 100:.1f}%")
        ap("")

    key_moves = set(review.get("keyMoves") or [])
    commented = [r for r in (review.get("moves") or [])
                 if r.get("comment") or r.get("moveNum") in key_moves]
    if commented:
        ap("## 四、关键手逐条讲解")
        ap("")
        for r in commented:
            color = "黑" if r.get("color") == 1 else "白"
            who = "玩家" if r.get("isPlayer") else "AI"
            ap(f"### 第 {r.get('moveNum')} 手 · {who}（{color}）{r.get('gtp')} · "
               f"{FLAG_LABEL.get(r.get('flag'), '')}")
            ap("")
            loss = r.get("lossPoints")
            wr = r.get("lossWinrate")
            detail = []
            if loss is not None:
                detail.append(f"损失 {loss} 目")
            if wr is not None:
                detail.append(f"胜率下降 {wr * 100:.1f}%")
            if r.get("winrateBefore") is not None:
                detail.append(f"胜率 {r['winrateBefore'] * 100:.1f}% → "
                              f"{(r.get('winrateAfter') or 0) * 100:.1f}%")
            if detail:
                ap(f"- 数据：{'，'.join(detail)}")
            best = r.get("bestMove") or {}
            if best.get("gtp"):
                var = " ".join((r.get("variationGtp") or [])[:6])
                ap(f"- 引擎首选：{best.get('gtp')}" + (f"，后续 {var}" if var else ""))
            c = r.get("comment") or {}
            if c.get("reason"):
                ap(f"- 为什么：{c['reason']}")
            if c.get("advice"):
                ap(f"- 怎么想：{c['advice']}")
            if c.get("maxim"):
                ap(f"- 记住这句：{c['maxim']}")
            ap("")

    if llm:
        ap("## 五、说明")
        ap("")
        if review.get("lowConfidence"):
            ap("- **精度提示**：本次分析未使用 KataGo（未安装或不可用），胜率与损失目数来自内置"
               "启发式引擎，仅作粗略参考。安装 KataGo 后重新生成报告可获得职业级精度。")
        if llm.get("used"):
            ap(f"- 讲解由大模型（{llm.get('model', '')}）基于引擎数据生成。")
        else:
            ap("- 本次未使用大模型（原因："
               f"{llm.get('error') or '未配置'}），讲解由模板根据引擎数据生成。")
        ap("- 所有胜率、目数损失、首选点均来自围棋引擎，未经大模型修改。")
        ap("")

    if sgf:
        ap("## 附录：棋谱（SGF）")
        ap("")
        ap("```sgf")
        ap(sgf)
        ap("```")
        ap("")
    return "\n".join(lines)
