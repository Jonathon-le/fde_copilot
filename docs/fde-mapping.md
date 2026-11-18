# 能力映射：这个项目证明了什么

> FDE（Forward Deployed Engineer）的核心不是"会写代码"，而是**能把模糊需求变成可交付、可解释、可维护的系统**。
> 下表每条都指向仓库里的具体证据，不写空泛的自评。

## 1. 需求拆解与迭代节奏

**能力**：把"做一个 AI 客服"拆成可验收的里程碑，每层先最小闭环再扩展。

**证据**：
- `LEARNING_PLAN.md`：M0–M10 的依赖顺序（先 CLI 跑通 → 再接 API → 再接界面）
- `MILESTONES/M0~M10.md`：每个里程碑有验收标准、自测问题，不是任务清单
- 实际问题：M5 先做对比 CLI（`scripts/retrieval_compare.py`），再改 `rag_ask`，避免一次性重写

**可讲的故事**：M6 任务 3 的第一版把入参写成 `AskResponse`，整个接口 422。拆步施工（空路由 → 检索 → 拒答 → 模型 → 错误码）让这类错误在第 1 步就暴露。

## 2. 快速原型 + 可验证的最小闭环

**能力**：每个能力都有独立的验证手段，不等整个系统搭完。

**证据**：
- `scripts/retrieval_compare.py`：三路检索并排对比
- `scripts/eval_retrieval.py` + `data/eval/queries.jsonl`：14 条评测集，四类覆盖
- `scripts/calibrate_relevance.py`：先把分数分布打出来，再决定要不要设阈值

**可讲的故事**：混合检索做完后，我以为会比 BM25 好；评测显示**持平**（hit@1 0.818，recall@4 1.000），于是修改了文档结论，而不是改数字。

## 3. 集成与协议细节（LLM / SSE / SQLite）

**能力**：处理第三方协议的实际坑，而不是只调 SDK 的 hello world。

**证据**：
- `app/llm/client.py`：`arguments` 是 JSON 字符串、`content` 为空但不能报错、SSE 逐行解析、`[DONE]` 终止
- `app/api/main.py` 的 `/ask/stream`：先发 `meta`、错误只能以事件形式返回、`X-Accel-Buffering: no`
- `app/db/connection.py`：WAL 模式、`row_factory`、线程限制

**可讲的故事**：探测阶段发现 DeepSeek 的工具调用响应 `content` 是空字符串而不是 `null`；如果不先探测，整个工具层会被误判成"响应无内容"。

## 4. 可解释性与可观测性

**能力**：系统出错时能回答"刚才发生了什么"，而不是只能复现。

**证据**：
- `app/obs/trace.py` + `traces` 表：四类调用都埋点，只存摘要
- `GET /metrics/summary`：p50/p95、状态分布
- `app/ui/streamlit_app.py` 观测页：trace 列表 + 详情
- 答案里的 `citations` 与 `hits`：用户能看到依据

**可讲的故事**：流式接口不能用请求级数据库连接写 trace——依赖在路由返回时关闭，而流在返回后才产生。这个坑写在 `record_trace_detached` 的注释里。

## 5. 风险识别与防护（安全边界）

**能力**：识别"模型输出不可信"带来的副作用风险，并在架构上约束。

**证据**：
- 写工具只生成 `pending_actions`，执行入口唯一：`app/agent/confirm.py::confirm_pending`
- 幂等：`status/result` 字段，重复 approve 返回首次结果
- 越权：`session_id` 不匹配返回 **404**（不用 403，避免探测存在性）
- 审计字段（`actor`）服务端固定；工具名白名单；参数二次校验
- 错误响应里不返回 `str(exc)`，只给 `error.type` + 固定文案

**可讲的故事**：模型提议 `customer_id="C999"` 时，格式校验拦不住——Pydantic 只看形状。最终靠仓库层的业务校验 + 人工确认。

## 6. 文档与交接

**能力**：让不了解项目的人能跑起来、能判断结论是否可信。

**证据**：
- `README.md`：3 行讲清是什么 + 快速开始 + 接口表 + 限制
- `docs/architecture.md`：分层图、两条时序图、10 条"决策 + 代价"
- `docs/demo.md`：5 分钟演示，每步标"花钱/不花钱"
- `scripts/demo_walkthrough.py`：一条命令跑完关键链路
- `MILESTONES/**` 与工程日志：踩坑过程与修正记录

## 7. 诚实与边界

**能力**：区分"实现了"和"验证过"，主动说明失效条件。

**证据**：
- `data/eval/report.md`：hybrid 与 BM25 持平，未观测到增益
- 相关性门槛结论：absent 与 in-domain 分数重叠（0.5842 vs 0.5728），**不可分**，默认关闭
- README「已知限制」：单机无鉴权、Agent 无跨轮历史、库外问题靠 prompt 拒答

---

## 如果被追问"哪里做得不够"

按优先级回答，不要含糊：

1. **评测规模太小**：34 chunk / 14 query，结论只能作为方向参考；扩大语料才能谈召回率。
2. **Agent 无会话记忆**：`session_id` 只绑定待确认单，多轮指代会丢。
3. **并发分层有限**：`/ask/stream` 已异步化，`/agent/chat` 仍同步；高并发还需异步数据库驱动。
4. **Agent 历史是短记忆**：只存 10 条/3000 字符自然语言，工具调用记录不入库。
5. **无鉴权/限流**：`session_id` 可伪造；上线前需要网关层补。
6. **相关性判定缺失**：门槛不可分，库外问题依赖模型自觉拒答。
