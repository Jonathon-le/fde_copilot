这是一个企业知识库 + 工单 Agent 项目。

当前完成 M1：知识库加载与中文分块。

在项目根目录运行：

```powershell
python -m scripts.check_env
python -m scripts.chunk_preview
python -m scripts.chunk_preview --json
```

`check_env` 做环境自检；`chunk_preview` 读取 `data/knowledge/`，按 Markdown 标题分节并切成带重叠的 chunk；`--json` 输出 JSONL，供后续 RAG 使用。

下一步进入 M2：工单数据库。
