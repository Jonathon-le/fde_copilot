"""把 data/knowledge/ 下的文档切块并写入向量库。

用法：
    python -m scripts.build_index
    python -m scripts.build_index --reset
"""

import argparse
import sys
import time

from app.config import get_settings
from app.rag.chunking import chunk_document
from app.rag.index import build_index
from app.rag.loaders import load_document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_index",
        description="加载知识库文档，切块后写入向量库。",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="先删除同名 collection 再重建",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = get_settings()
    root = settings.knowledge_dir

    # 先校验输入，再动向量库：参数错的时候不应该产生任何副作用
    if not root.exists():
        print(f"知识库目录不存在：{root}", file=sys.stderr)
        return 1
    if not root.is_dir():
        print(f"不是目录：{root}", file=sys.stderr)
        return 1

    docs = load_document(str(root))
    if not docs:
        print(f"目录中没有可加载的 .md / .txt 文档：{root}", file=sys.stderr)
        return 1

    chunks: list[dict] = []
    for doc in docs:
        chunks.extend(chunk_document(doc))
    if not chunks:
        print("文档已加载，但没有切出任何 chunk", file=sys.stderr)
        return 1

    started = time.perf_counter()
    try:
        written = build_index(chunks, reset=args.reset)
    except Exception as exc:  # noqa: BLE001
        # CLI 是最后一道翻译层：库里的异常类型五花八门（Chroma 自家的层级、
        # 模型目录缺失的 OSError），在这里统一转成「人话 + 退出码」。
        # 注意这个宽泛的 except 只允许出现在 main() 里，库函数里绝不能这么写。
        print(f"写入向量库失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    elapsed = time.perf_counter() - started

    print(f"知识库目录：{root}")
    print(f"文档数：{len(docs)}")
    print(f"chunk 总数：{len(chunks)}")
    print(f"写入数量：{written}")
    print(f"耗时：{elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
