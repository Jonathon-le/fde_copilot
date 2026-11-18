# 检索评测报告

- 生成时间：2026-11-18T16:31:21+08:00
- 评测集：`data\eval\queries.jsonl`（14 条，其中计分 11 条，absent 3 条）
- recall 的 k = 4

## 总体指标

| 模式 | hit@1 | recall@4 |
|---|---|---|
| vector | 0.818 | 0.909 |
| bm25 | 0.818 | 1.000 |
| hybrid | 0.818 | 1.000 |

## 按类别拆分

| 模式 | 类别 | hit@1 | recall@4 |
|---|---|---|---|
| vector | term | 1.000 | 1.000 |
| vector | paraphrase | 0.500 | 0.750 |
| vector | multi_doc | 1.000 | 1.000 |
| bm25 | term | 1.000 | 1.000 |
| bm25 | paraphrase | 0.750 | 1.000 |
| bm25 | multi_doc | 0.667 | 1.000 |
| hybrid | term | 1.000 | 1.000 |
| hybrid | paraphrase | 0.750 | 1.000 |
| hybrid | multi_doc | 0.667 | 1.000 |

## 库外问题的检索行为

`absent` 类问题不计入 hit/recall（它们本来就没有正确答案），
这里看的是**检索器会不会硬塞结果**：返回条数越少越好。

| 模式 | absent 问题返回的条数（应为 0 才理想） |
|---|---|
| vector | [4, 4, 4] |
| bm25 | [0, 4, 0] |
| hybrid | [4, 4, 4] |

## 相关性门槛校准

分数**不可分**：

- absent 最高 vector = 0.5842，in-domain 最低 = 0.5728
- absent 最高 bm25 = 2.885，in-domain 最低 = 1.252

结论：任何单一阈值都会在两侧各错一部分，因此门槛默认关闭（`RELEVANCE_MIN_SCORE` 不设置）。
可选改进：扩大知识库与评测集后重新校准；或改用 LLM 判定相关性。
