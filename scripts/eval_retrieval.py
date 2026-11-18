"""检索评测：hit@1 / recall@k。"""

import argparse
import json
import sys
from pathlib import Path

from app.rag.hybrid import search_hybrid
from app.rag.index import IndexEmptyError

MODES = ("vector", "bm25", "hybrid")
VALID_CATEGORIES = ("term", "paraphrase", "multi_doc", "absent")
SCORED_CATEGORIES = ("term", "paraphrase", "multi_doc")


def norm_section(section: str) -> str:
    return (section or "").lstrip("#").strip()


def load_queries(path: Path) -> list[dict]:
    queries = []
    with path.open(encoding="utf-8") as f:
        #按行遍历文件Path
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno} JSON 解析失败：{exc}") from exc
           #每条记录必须含id，query，category三个key否则报错缺少字段
            for key in ("id", "query", "category"):
                if key not in item:
                    raise ValueError(f"{path}:{lineno} 缺少字段 {key!r}")
            #category必须在预定义合法集合VALID_CATEGORIES里面否则抛错
            if item["category"] not in VALID_CATEGORIES:
                raise ValueError(f"{path}:{lineno} 未知 category {item['category']!r}")
            item.setdefault("expect", [])
            #全部校验玩则加入结果列表
            queries.append(item)
    return queries

#判断一条检索结果hit，是否命中任意一条预期标准答案expect
def match(hit: dict, expects: list[dict]) -> bool:
   #any()：只要有一条expect匹配成功，返回true，全部不匹配返回False
    return any(
        #检索结果的id等于预期结果的id
        hit["doc_id"] == e.get("doc", "")
        #对章节文本归一化后章节匹配，两条件同时成立则整体返回true
        and norm_section(hit["section"]) == norm_section(e.get("section", ""))
        for e in expects
    )


def evaluate(mode: str, queries: list[dict], k: int) -> tuple[list[dict], list[int]]:
    rows, absent_counts = [], []
    for q in queries:
        hits = search_hybrid(q["query"], top_k=k, mode=mode)
        if q["category"] == "absent":
            absent_counts.append(len(hits))
            continue
        rows.append(
            {
                "id": q["id"],
                "category": q["category"],
                "hit1": bool(hits) and match(hits[0], q["expect"]),
                "recall": any(match(h, q["expect"]) for h in hits),
            }
        )
    return rows, absent_counts


def ratio(values: list[bool]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize(rows: list[dict]) -> tuple[float, float, dict[str, tuple[float, float]]]:
    by_cat = {}
    for cat in SCORED_CATEGORIES:
        sub = [r for r in rows if r["category"] == cat]
        if sub:
            by_cat[cat] = (ratio([r["hit1"] for r in sub]), ratio([r["recall"] for r in sub]))
    return ratio([r["hit1"] for r in rows]), ratio([r["recall"] for r in rows]), by_cat


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eval_retrieval", description="对比三种检索模式的 hit@1 / recall@k。")
    parser.add_argument("--k", type=int, default=4, help="召回条数，默认 4")
    parser.add_argument("--file", default="data/eval/queries.jsonl", help="评测集路径")
    parser.add_argument("--show-misses", action="store_true", help="打印未命中的 query")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.k < 1:
        parser.error(f"--k 必须 >= 1，当前 {args.k}")

    path = Path(args.file)
    if not path.exists():
        print(f"错误：评测集不存在：{path}", file=sys.stderr)
        return 1

    try:
        queries = load_queries(path)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    scored = [q for q in queries if q["category"] != "absent"]
    if not scored:
        print("错误：没有可计分的 query", file=sys.stderr)
        return 1

    results = {}
    try:
        for mode in MODES:
            results[mode] = evaluate(mode, queries, args.k)
    except IndexEmptyError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    summary = {mode: summarize(results[mode][0]) for mode in MODES}

    print(f"评测集：{len(queries)} 条（有效 {len(scored)} / 库外 {len(queries) - len(scored)}）， k={args.k}")
    print()
    print(f"{'模式':<10}{'hit@1':>8}{'recall@%d' % args.k:>12}")
    for mode in MODES:
        hit1, recall, _ = summary[mode]
        print(f"{mode:<10}{hit1:>8.3f}{recall:>12.3f}")

    print()
    print(f"{'分类':<12}{'模式':<10}{'hit@1':>8}{'recall':>8}")
    for cat in SCORED_CATEGORIES:
        for mode in MODES:
            by_cat = summary[mode][2]
            if cat not in by_cat:
                continue
            hit1, recall = by_cat[cat]
            print(f"{cat:<12}{mode:<10}{hit1:>8.3f}{recall:>8.3f}")

    print()
    print("库外问题（不计入 hit/recall）")
    print(f"{'模式':<10}{'平均返回':>10}{'0命中率':>10}")
    for mode in MODES:
        _, counts = results[mode]
        if not counts:
            continue
        avg = sum(counts) / len(counts)
        zero = sum(1 for c in counts if c == 0) / len(counts)
        print(f"{mode:<10}{avg:>10.2f}{zero:>9.0%}")

    if args.show_misses:
        print()
        print("未命中明细")
        for mode in MODES:
            rows, _ = results[mode]
            missed = [r["id"] for r in rows if not r["recall"]]
            print(f"  {mode:<8} recall 未命中：{', '.join(missed) or '—'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
