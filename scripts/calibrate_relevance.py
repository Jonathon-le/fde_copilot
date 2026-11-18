"""相关性门槛校准。

产出：每条评测 query 的 vector top1 与 BM25 top1 分数，按类别分组，
并判断"是否存在能把 absent 与 in-domain 分开的阈值"。

用法：
    python -m scripts.calibrate_relevance
    python -m scripts.calibrate_relevance --file data/eval/queries.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.rag.bm25_index import search_bm25
from app.rag.retriever import search_vector

SCORED_CATEGORIES = ("term", "paraphrase", "multi_doc")


def load_queries(path: Path) -> list[dict]:
    rows = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno} 不是合法 JSON：{exc}") from exc
    return rows


def collect(queries: list[dict]) -> list[dict]:
    """对每条 query 取两路 top1 分数。

    vector 用原始余弦分（1-dist），BM25 用原始分数。
    **不要用 hybrid 融合后的 score**：那是 RRF，和阈值没有可比性。
    """
    rows = []
    for q in queries:
        vec_hits = search_vector(q["query"], top_k=1)
        bm25_hits = search_bm25(q["query"], top_k=1)
        rows.append({
            "id": q.get("id", "?"),
            "category": q.get("category", "?"),
            "query": q["query"],
            "vector": float(vec_hits[0]["score"]) if vec_hits else 0.0,
            "bm25": float(bm25_hits[0]["score"]) if bm25_hits else 0.0,
            "top_doc": vec_hits[0]["doc_id"] if vec_hits else "-",
        })
    return rows


def verdict(rows: list[dict]) -> dict:
    """判断分数是否可分。

    可分 = absent 的最高分 < in-domain 的最低分。
    重叠时任何阈值都会二选一地犯错：要么误拒正常问题，要么放过库外问题。
    """
    absent = [r for r in rows if r["category"] == "absent"]
    other = [r for r in rows if r["category"] != "absent"]
    if not absent or not other:
        return {"separable": False, "reason": "缺少 absent 或 in-domain 样本"}
    a_max_vec = max(r["vector"] for r in absent)
    o_min_vec = min(r["vector"] for r in other)
    a_max_bm = max(r["bm25"] for r in absent)
    o_min_bm = min(r["bm25"] for r in other)
    return {
        "separable": a_max_vec < o_min_vec,
        "absent_max_vector": round(a_max_vec, 4),
        "indomain_min_vector": round(o_min_vec, 4),
        "gap": round(o_min_vec - a_max_vec, 4),
        "absent_max_bm25": round(a_max_bm, 3),
        "indomain_min_bm25": round(o_min_bm, 3),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="校准相关性门槛")
    parser.add_argument("--file", type=Path,
                        default=Path("data/eval/queries.jsonl"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.file.exists():
        print(f"找不到评测文件：{args.file}", file=sys.stderr)
        return 2
    rows = collect(load_queries(args.file))

    print(f"{'category':11}{'id':5}{'vector':9}{'bm25':8}{'top_doc':14}query")
    for r in sorted(rows, key=lambda x: (x["category"], x["vector"])):
        print(f"{r['category']:11}{r['id']:5}{r['vector']:<9.4f}{r['bm25']:<8.3f}"
              f"{r['top_doc']:14}{r['query'][:24]}")

    v = verdict(rows)
    print("\n--- 结论 ---")
    if v.get("separable"):
        mid = (v["absent_max_vector"] + v["indomain_min_vector"]) / 2
        print(f"可分：absent 最高 {v['absent_max_vector']} < in-domain 最低 {v['indomain_min_vector']}")
        print(f"建议阈值取中点 {mid:.4f}（设置 RELEVANCE_MIN_SCORE={mid:.2f} 启用）")
    else:
        print("**不可分**：分数区间重叠，不存在能同时满足两侧的阈值。")
        print(f"  absent 最高 vector = {v.get('absent_max_vector')}，"
              f"in-domain 最低 vector = {v.get('indomain_min_vector')}")
        print(f"  absent 最高 bm25 = {v.get('absent_max_bm25')}，"
              f"in-domain 最低 bm25 = {v.get('indomain_min_bm25')}")
        print("  建议：保持 RELEVANCE_MIN_SCORE 未设置（门槛关闭），"
              "扩大知识库/评测集后再校准；或改用 LLM 判定相关性。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
