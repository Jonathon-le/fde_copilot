# 5 分钟演示流程

每一步都标了 **💰花钱 / 💤不花钱**。演示前建议先跑一遍，确认输出与文中一致。

## 0. 准备（一次性，💤）

```powershell
cd D:\MyPython\fde_copilot
pip install -r requirements.txt

$env:LLM_API_KEY="sk-..."          # 不要提交、不要截图
$env:LLM_BASE_URL="https://api.deepseek.com/v1"
$env:LLM_MODEL="deepseek-chat"

python -m scripts.build_index      # 建向量库（34 chunks）
python -m scripts.ticket_cli init-db
```

预期：`data/chroma/` 出现，`data/app.db` 里有 7 张表（customers/orders/tickets/ticket_events/pending_actions/traces/agent_messages）+ 种子工单。

## 1. 启动（💤）

```powershell
# 终端 A
python -m uvicorn app.api.main:app --reload
# 终端 B
python -m streamlit run app/ui/streamlit_app.py
```

预期：
- `http://127.0.0.1:8000/docs` 能看到 11 个接口，全部带中文说明和示例
- `http://127.0.0.1:8501` 出现四个标签页，侧边栏显示"服务正常 · 索引 34 条"

## 2. 契约演示（💤）

打开 `/docs`，展开 `POST /ask` → **Try it out** → 输入：

```json
{"question": "红字发票怎么开", "top_k": 4, "mode": "hybrid"}
```

预期：200，`answer` 里有 `[1]`，`citations[0]` 是 `发票与报销 / 4. 红字发票（退票 / 红冲）`。

再试一个故意错的：`{"question": ""}` → **422**，格式统一为 `{"error": {...}}`。

## 3. 流式问答（💰 一次模型调用）

```powershell
python -c "import httpx;[print(l) for l in httpx.stream('POST','http://127.0.0.1:8000/ask/stream',json={'question':'退款多久到账','top_k':3},timeout=120).iter_lines() if l.startswith('data:')][:3]"
```

更稳妥的方式是在界面上看：知识问答页输入"退款多久到账"，文字逐段出现，下方能看到引用与命中片段。

预期：约 2 秒，`meta` 先到，然后 50+ 个 `delta`，最后 `done` 带 citations。

## 4. 工单读写（💤）

```powershell
# 列表
python -c "import httpx,json;print(json.dumps(httpx.get('http://127.0.0.1:8000/tickets?status=open').json(),ensure_ascii=False)[:200])"

# 不存在 → 404 统一结构
python -c "import httpx;print(httpx.get('http://127.0.0.1:8000/tickets/TKT-NOPE').status_code, httpx.get('http://127.0.0.1:8000/tickets/TKT-NOPE').json())"

# 创建 → 201 + Location
python -c "import httpx;r=httpx.post('http://127.0.0.1:8000/tickets',json={'customer_id':'C001','title':'演示工单','description':'演示用'});print(r.status_code, r.headers.get('location'))"
```

预期：`404 {'error': {'type': 'not_found', ...}}`；创建返回 `201 /tickets/TKT-XXXXXXXX`。

## 5. Agent：读工具自动执行（💰 一次模型调用）

界面 → **Agent** 标签页 → 输入：

```text
订单 O001 现在什么状态
```

预期：模型自动调 `get_order`，展开"工具调用过程"能看到 1 步、耗时；答案是"已付款 / 299 元 / 顺丰 SF1234567890"。

## 6. Agent：写工具必须确认（💰 一次 + 💤）

同一个输入框输入：

```text
订单 O001 收到时破了，帮我登记一张退款工单
```

预期：
1. 模型调用 `get_order` → 可能再检索政策 → 提议 `create_ticket`
2. 页面出现"待确认操作"卡片：工具名、完整参数 JSON、有效期
3. **此时数据库里没有新工单**（这是关键演示点）
4. 点「❌ 拒绝」→ 状态 `rejected`，仍无新工单

点「✅ 同意执行」则会真正建单，并在工单页看到 `events[0].actor = api`。

**重复确认**：连点两次同意（或用接口重放同一 `pending_id`），只会产生一张工单——幂等。

## 7. 观测（💤）

界面 → **📈 观测** 标签页。

预期：
- 总调用数、p50/p95 延迟、状态分布（`ok / refused / pending / rejected`）
- 下方 trace 表格：类型、状态、耗时、步数
- 选中一条看详情：`payload` 里是命中数、top_doc、引用数等摘要（**不含答案全文**）

## 8. 测试与评测（💤，全部不联网）

```powershell
python -m pytest -q                    # 13 passed（含 8 并发流式测试）
python -m scripts.eval_retrieval       # 三模式 hit@1 / recall@4 表
python -m scripts.calibrate_relevance  # 输出"不可分"的校准结论
python -m scripts.eval_report          # 重新生成 data/eval/report.md
```

## 9. 半自动演示脚本（💰 视步骤而定）

```powershell
python -m scripts.demo_walkthrough
```

会依次跑：健康检查 → `/ask` → `/agent/chat` →（有 pending 时）拒绝 → trace 汇总，
每步打印结论与耗时，任一步失败返回非零退出码。

## 演示时最容易被问到的问题

| 问题 | 回答要点 |
|---|---|
| 混合检索比单路好吗？ | **持平**。hit@1 0.818 三者相同，hybrid 的 recall@4 与 BM25 相同（1.000）。34 chunk 太小，没观测到融合增益 |
| 怎么防模型乱建单？ | 写工具只落 `pending_actions`，执行入口唯一（`confirm_pending`）；actor 服务端固定；参数二次校验 |
| 重复点击确认会怎样？ | 幂等：第二次返回第一次的 result，不重复执行；`failed` 不允许重试 |
| 用户 A 能确认用户 B 的单吗？ | 不能，`session_id` 不匹配返回 404（不用 403，避免探测 id 是否存在） |
| 库外问题怎么办？ | 目前靠 prompt 拒答；相关性门槛因分数重叠默认关闭，结论写在 `data/eval/report.md` |
