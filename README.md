# FDE Support Copilot

企业知识库 + 工单 Agent：本地知识库检索、工单管理、可人工确认的 Agent 写操作。

当前进度：**M0–M8 完成**（RAG 检索 → 混合召回 → HTTP 服务 → Agent 与确认 → 流式界面）。

## 快速开始

```powershell
# 1) 依赖
pip install -r requirements.txt

# 2) 环境变量（不要提交到 git）
$env:LLM_API_KEY="sk-..."
$env:LLM_BASE_URL="https://api.deepseek.com/v1"
$env:LLM_MODEL="deepseek-chat"

# 3) 建向量索引 + 建库
python -m scripts.build_index
python -m scripts.ticket_cli init-db

# 4) 启动 API
python -m uvicorn app.api.main:app --reload
#    接口文档：http://127.0.0.1:8000/docs

# 5) 启动界面（另开一个终端）
python -m streamlit run app/ui/streamlit_app.py
```

## 接口一览

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 服务 / 数据库 / 索引状态 |
| POST | `/ask` | 知识库问答（返回答案 + 引用） |
| POST | `/ask/stream` | 同上，SSE 流式 |
| GET | `/tickets` | 工单列表（可按状态 / 客户过滤） |
| GET | `/tickets/{id}` | 工单详情 + 事件时间线 |
| POST | `/tickets` | 创建工单（201 + Location） |
| PATCH | `/tickets/{id}/status` | 更新状态（非法流转 409） |
| POST | `/agent/chat` | Agent 对话（可提议写操作） |
| POST | `/agent/confirm` | 确认 / 拒绝待执行操作（幂等） |

## 设计要点

- **检索**：向量 + BM25 经 RRF 融合；34 chunk 评测集上 hybrid 与 BM25 持平（见 `scripts/eval_retrieval.py`）。
- **错误**：领域异常经 `app/api/errors.py` 统一映射为 404 / 400 / 409 / 422 / 502 / 503，响应体固定 `{"error": {"type", "message"}}`。
- **Agent 安全**：读工具自动执行；写工具只生成 `pending_actions`，必须经 `/agent/confirm` 才落库，重复确认幂等。
- **前端**：Streamlit 只作为 HTTP 客户端，不 import 业务模块。

## 已知限制

- 库外问题没有相关性门槛，仍会返回 top-k 片段（由模型按 prompt 拒答）。
- Agent 没有跨轮对话历史，`session_id` 只用于绑定待确认操作。
- 本地单机项目，未做鉴权、限流与多用户隔离。
