"""生成评测报告 data/eval/report.md。

报告必须是**脚本生成**的：手写的数字会过期，而且没人能复现。
内容三块：
1. 三种检索模式的 hit@1 / recall@k（复用 eval_retrieval 的实现，避免两套口径）
2. 按类别拆分
3. 相关性门槛校准结论（复用 calibrate_relevance 的计算）

用法：
    python -m scripts.eval_report
    python -m scripts.eval_report --k 4 --out data/eval/report.md
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from scripts.calibrate_relevance import collect, load_queries, verdict
from scripts.eval_retrieval import MODES, evaluate, summarize


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成评测报告")
    parser.add_argument("--k", type=int, default=4)
    parser.add_argument("--file", type=Path, default=Path("data/eval/queries.jsonl"))
    parser.add_argument("--out", type=Path, default=Path("data/eval/report.md"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.file.exists():
        print(f"找不到评测文件：{args.file}", file=sys.stderr)
        return 2

    queries = load_queries(args.file)
    scored = [q for q in queries if q["category"] != "absent"]

    lines: list[str] = []
    lines.append("# 检索评测报告")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}")
    lines.append(f"- 评测集：`{args.file}`（{len(queries)} 条，其中计分 {len(scored)} 条，"
                 f"absent {len(queries) - len(scored)} 条）")
    lines.append(f"- recall 的 k = {args.k}")
    lines.append("")
    lines.append("## 总体指标")
    lines.append("")
    lines.append(f"| 模式 | hit@1 | recall@{args.k} |")
    lines.append("|---|---|---|")

    results = {}
    for mode in MODES:
        rows, absent_counts = evaluate(mode, queries, args.k)
        hit1, recall, by_cat = summarize(rows)
        results[mode] = (rows, absent_counts, hit1, recall, by_cat)
        lines.append(f"| {mode} | {hit1:.3f} | {recall:.3f} |")

    lines.append("")
    lines.append("## 按类别拆分")
    lines.append("")
    lines.append(f"| 模式 | 类别 | hit@1 | recall@{args.k} |")
    lines.append("|---|---|---|---|")
    for mode in MODES:
        _, _, _, _, by_cat = results[mode]
        for cat, (h, r) in by_cat.items():
            lines.append(f"| {mode} | {cat} | {h:.3f} | {r:.3f} |")

    lines.append("")
    lines.append("## 库外问题的检索行为")
    lines.append("")
    lines.append("`absent` 类问题不计入 hit/recall（它们本来就没有正确答案），")
    lines.append("这里看的是**检索器会不会硬塞结果**：返回条数越少越好。")
    lines.append("")
    lines.append("| 模式 | absent 问题返回的条数（应为 0 才理想） |")
    lines.append("|---|---|")
    for mode in MODES:
        _, absent_counts, _, _, _ = results[mode]
        lines.append(f"| {mode} | {absent_counts} |")

    rows = collect(queries)
    v = verdict(rows)
    lines.append("")
    lines.append("## 相关性门槛校准")
    lines.append("")
    if v.get("separable"):
        lines.append(f"分数**可分**：absent 最高 {v['absent_max_vector']} < "
                     f"in-domain 最低 {v['indomain_min_vector']}。")
        lines.append("")
        lines.append(f"建议阈值取中点，设置 `RELEVANCE_MIN_SCORE` 后生效。")
    else:
        lines.append("分数**不可分**：")
        lines.append("")
        lines.append(f"- absent 最高 vector = {v.get('absent_max_vector')}，"
                     f"in-domain 最低 = {v.get('indomain_min_vector')}")
        lines.append(f"- absent 最高 bm25 = {v.get('absent_max_bm25')}，"
                     f"in-domain 最低 = {v.get('indomain_min_bm25')}")
        lines.append("")
        lines.append("结论：任何单一阈值都会在两侧各错一部分，因此门槛默认关闭"
                     "（`RELEVANCE_MIN_SCORE` 不设置）。")
        lines.append("可选改进：扩大知识库与评测集后重新校准；或改用 LLM 判定相关性。")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已生成 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
