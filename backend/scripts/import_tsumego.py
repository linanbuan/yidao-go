"""导入死活题 SGF 到平台题库。

用法：
    python scripts/import_tsumego.py <文件或目录>... [选项]

选项：
    --goal live|kill   目标是做活还是杀棋（缺省时从标题/注释里猜，猜不出就跳过）
    --difficulty N     难度 1~9（对齐平台客观打分的 1~9 与五档体系），默认 3
    --family 名称      分类名（默认「导入」，会显示在练习页的筛选里）
    --source 文本      出处与许可说明（**强烈建议写清**，例如
                       "《玄玄棋经》公共领域，SGF 由本人录入"）
    --replace          覆盖同 id 的已有题目（默认跳过，便于重复导入）
    --dry-run          只解析并打印结果，不写库

题目格式约定（业界死活题 SGF 的通行写法）：
    根节点 AB[]/AW[] 摆子，PL[B]/PL[W] 指明先走方（缺省看主线第一手颜色）；
    主线 = 正解；从第一手分叉的兄弟分支 = 失败图；C[] 注释 = 讲解。

关于开源题集：项目没有内置任何第三方受版权保护的题目。古典题书
（《玄玄棋经》1349、《官子谱》1690、《发阳论》1713）本身已进入公共领域，
但网上流传的 SGF 录入本多为论坛/个人整理，**许可证需要你自己确认**再导入，
并把确认结果写进 --source，这样练习页与题目详情里会一直显示出处。
平台内置的 21 道基本形题目是本项目用穷举搜索自己推导的，不依赖外部题集。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.database import init_db                                   # noqa: E402
from app.tsumego.sgfimport import import_sgf_text, save_problems    # noqa: E402


def collect_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(p.rglob("*.sgf")))
            files.extend(sorted(p.rglob("*.SGF")))
        elif p.is_file():
            files.append(p)
        else:
            print(f"[跳过] 路径不存在：{p}")
    seen, out = set(), []
    for f in files:
        key = f.resolve()
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="导入死活题 SGF")
    ap.add_argument("paths", nargs="+", help="SGF 文件或目录")
    ap.add_argument("--goal", choices=["live", "kill"], default=None)
    ap.add_argument("--difficulty", type=int, default=3)
    ap.add_argument("--family", default="导入")
    ap.add_argument("--source", default="")
    ap.add_argument("--replace", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not 1 <= args.difficulty <= 9:
        print("[错误] --difficulty 必须在 1~9 之间（对齐五档：入门1-2/初级3/中级4-5/高级6-7/段位8-9）")
        return 2

    files = collect_files(args.paths)
    if not files:
        print("[错误] 没有找到任何 SGF 文件")
        return 2

    if not args.dry_run:
        init_db()

    total_ok = total_skip = 0
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"[错误] 读不了 {f}: {exc}")
            continue
        problems, skipped = import_sgf_text(
            text, origin=f.name, goal=args.goal, difficulty=args.difficulty,
            family=args.family, source=args.source or f"导入自 {f.name}（许可由导入者确认）")
        print(f"\n=== {f} ===  解析出 {len(problems)} 题，跳过 {len(skipped)} 条")
        for p in problems[:200]:
            correct = [ln for ln in p["lines"] if ln["result"] == "correct"]
            wrong = [ln for ln in p["lines"] if ln["result"] == "wrong"]
            print(f"  [{p['goal']:>4}] {p['title']}  {p['size']}路  "
                  f"{'黑' if p['toMove'] == 1 else '白'}先  "
                  f"正解 {len(correct)} 条 / 失败图 {len(wrong)} 条  id={p['pid']}")
        if len(problems) > 200:
            print(f"  …（另有 {len(problems) - 200} 题未列出）")
        for reason in skipped[:20]:
            print(f"  [跳过] {reason}")
        if len(skipped) > 20:
            print(f"  …（另有 {len(skipped) - 20} 条跳过原因未列出）")
        total_ok += len(problems)
        total_skip += len(skipped)
        if args.dry_run:
            continue
        saved = save_problems(problems, replace=args.replace)
        print(f"  已写入 {saved} 题（同 id 已存在且未加 --replace 的会跳过）")

    print(f"\n合计：可导入 {total_ok} 题，跳过 {total_skip} 条。"
          + ("（dry-run，未写库）" if args.dry_run else ""))
    return 0 if total_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
