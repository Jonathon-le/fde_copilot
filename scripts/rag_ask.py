"""基于企业知识库回答问题。

用法：
    python -m scripts.rag_ask "退款政策是什么？"
    python -m scripts.rag_ask "退款政策是什么？" --top-k 6
    python -m scripts.rag_ask "退款政策是什么？" --show-context
    python -m scripts.rag_ask "退款政策是什么？" --retrieval-only
    python -m scripts.rag_ask "退款政策是什么？" --mock
    python -m scripts.rag_ask "退款政策是什么？" --mode bm25
    python -m scripts.rag_ask "退款政策是什么？" --mode vector --retrieval-only

退出码：0 成功；1 配置/网络/知识库为空；2 参数错误。
"""

import argparse
import re
import sys
from pathlib import Path

from app.llm.client import LLMError, chat
from app.rag.prompt import build_messages
from app.rag.index import IndexEmptyError
from app.rag.hybrid import search_hybrid

# 回答里的引用编号，形如 [1]、[12]
CITATION_RE = re.compile(r"\[(\d+)\]")

NO_CONTEXT_ANSWER = "资料中没有提到这个问题。"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rag_ask",
        description="先检索知识库，再让大模型基于检索结果回答。",
    )
    parser.add_argument("question", help="要问的问题")
    parser.add_argument(
        "--top-k",
        type=int,
        default=4,
        help="检索的片段数量，默认 4",
    )
    parser.add_argument(
        "--show-context",
        action="store_true",
        help="把命中的片段和分数打印到 stderr",
    )
    parser.add_argument(
        "--retrieval-only",
        action="store_true",
        help="只做检索并打印片段，不调用大模型",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="不联网，打印固定回答，用于验证 CLI 管线",
    )
    parser.add_argument(
        "--mode",
        choices=["vector","bm25","hybrid"],
        default="hybrid",
        help="检索模式,默认hybrid",
    )
    return parser


def _short_section(section: str) -> str:
    """把 '## 5. 退款时效与路径' 显示成 '5. 退款时效与路径'。"""
    return section.lstrip("#").strip()


def _print_context(hits: list[dict]) -> None:
    """--show-context：片段和分数走 stderr，不污染 stdout。"""
    print(f"[上下文] 命中 {len(hits)} 条：", file=sys.stderr)
    for i, hit in enumerate(hits, start=1):
        print(
            f"  [{i}] {hit['score']:.4f}  {hit['title']} / {_short_section(hit['section'])}",
            file=sys.stderr,
        )


def _print_hits(hits: list[dict]) -> None:
    """--retrieval-only：片段正文走 stdout，这是本次调用的「数据」。"""
    for i, hit in enumerate(hits, start=1):
        print(f"[{i}] {hit['title']} / {_short_section(hit['section'])}  score={hit['score']:.4f}")
        print(hit["text"].strip())
        print()


def _report_citations(content: str, hits: list[dict]) -> None:
    """解析回答里的 [n]，把来源打到 stderr，并检查是否越界。"""
    cited: list[int] = []
    for match in CITATION_RE.finditer(content):
        n = int(match.group(1))
        if n not in cited:
            cited.append(n)

    if not cited:
        print("提示：回答中没有出现引用编号。", file=sys.stderr)
        return

    parts = []
    for n in cited:
        if 1 <= n <= len(hits):
            hit = hits[n - 1]
            title = Path(hit["source_path"]).stem
            parts.append(f"[{n}] {title} / {_short_section(hit['section'])}")
        else:
            # 越界只提示、不中断：回答已经打出来了，用户自己判断
            print(
                f"提示：回答引用了不存在的片段编号 [{n}]（本次只检索到 {len(hits)} 条）。",
                file=sys.stderr,
            )
    if parts:
        print("引用：" + "；".join(parts), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.question.strip():
        parser.error("问题不能为空")
    if args.top_k < 1:
        # 库函数里也有一道同样的检查：这里管用户输入，那里管别的调用方
        parser.error(f"--top-k 必须 >= 1，当前 {args.top_k}")

    try:
        hits = search_hybrid(args.question, top_k=args.top_k,mode=args.mode)
    except IndexEmptyError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if args.show_context:
        _print_context(hits)

    if args.retrieval_only:
        _print_hits(hits)
        return 0

    # 没有检索到任何资料时不调模型：省钱、确定、避免模型自由发挥
    if not hits:
        print(NO_CONTEXT_ANSWER)
        return 0

    if args.mock:
        print(f"[mock] 检索到 {len(hits)} 条资料，跳过模型调用。")
        first = hits[0]
        print(
            f"这是 mock 回答：{first['title']} / {_short_section(first['section'])}"
            " 提到了相关内容 [1]。"
        )
        return 0

    messages = build_messages(args.question, hits)
    try:
        result = chat(messages)
    except LLMError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    print(result["content"])
    _report_citations(result["content"], hits)
    return 0


if __name__ == "__main__":
    sys.exit(main())
