"""相关性门槛（可配置，默认关闭）。

为什么默认关闭：在当前的评测集上（34 chunk / 14 query），
absent 与 in-domain 的 vector top1 分数是**重叠**的：

    absent   max = 0.5842   （q13 能开英文发票吗）
    in-domain min = 0.5728  （q06 钱什么时候退回我卡里）

不存在一个能完美分开的阈值。拍一个 0.58 会把 q06 误拒，同时放过 q13。
所以这里做成开关：默认 0 = 关闭；想实验时设 RELEVANCE_MIN_SCORE=0.6 之类。

开销提醒：开启后每条 query 多一次 embedding（search_vector top1），
这是为了拿到"原始余弦分"——hybrid 融合后的 score 是 RRF，量纲不同，不能拿来比阈值。
"""

from __future__ import annotations

import os

from app.rag.retriever import search_vector


def min_score() -> float:
    raw = os.getenv("RELEVANCE_MIN_SCORE", "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        # 配置写错时按"关闭"处理：宁可多调用一次模型，也不要静默拒答所有问题
        return 0.0


def is_relevant(query: str) -> tuple[bool, float]:
    """返回 (是否相关, top1 原始余弦分)。

    门槛关闭时不做额外检索，返回 (True, 0.0)。
    门槛开启但索引为空时，让 IndexEmptyError 继续往上抛（由 API 层转 503）。
    """
    threshold = min_score()
    if threshold <= 0:
        return True, 0.0
    hits = search_vector(query, top_k=1)
    top = float(hits[0]["score"]) if hits else 0.0
    return top >= threshold, top
