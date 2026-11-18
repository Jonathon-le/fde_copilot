"""Streamlit 前端：企业客服 Copilot。

设计原则：**前端只是 API 的客户端**。
这个文件不 import app.rag / app.db / app.agent 里的任何东西——
所有能力都走 HTTP。这样 M9 换前端、加鉴权、多实例部署时都不用动业务代码。

运行：
    python -m streamlit run app/ui/streamlit_app.py
（需要 API 已经在 8000 端口跑起来：python -m uvicorn app.api.main:app）

Streamlit 的心智模型，第一次写要记住三点：
1. **脚本从上到下重跑**：每次点按钮/输入框回车，整个文件重新执行一遍。
2. **状态要自己存**：普通变量重跑就没了，所以对话历史、待确认单必须放
   st.session_state（一个跨重跑存活的 dict）。
3. **按钮返回 True 只在这一轮**：`if st.button(...):` 里的代码只在点击的那次重跑执行。
"""

from __future__ import annotations

import json
import os
import uuid

import httpx
import streamlit as st

API_BASE = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
# /agent/chat 会多轮调模型，/ask/stream 是长连接，超时给宽一点
TIMEOUT = 180.0

st.set_page_config(
    page_title="FDE Support Copilot",
    page_icon="🛎️",
    layout="wide",
)


class ApiError(Exception):
    """后端返回 4xx/5xx 时抛这个，统一在界面上显示。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def api(method: str, path: str, **kwargs) -> dict:
    """所有同步请求的统一入口。错误统一成可读中文，不把 traceback 甩给用户。"""
    try:
        resp = httpx.request(method, f"{API_BASE}{path}", timeout=TIMEOUT, **kwargs)
    except httpx.RequestError:
        st.error(
            f"连不上 API（{API_BASE}）。先在另一个终端运行：\n\n"
            "`python -m uvicorn app.api.main:app --reload`"
        )
        # st.stop() 在 Streamlit 运行时里会抛 StopException 中断本次脚本；
        # 但它只保证在 runtime 里生效。补一个 raise，避免在普通 python 环境
        # （比如单元测试或误用 import）下继续往下走、拿到未赋值的 resp。
        st.stop()
        raise ApiError(0, "无法连接 API")

    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text[:300]}

    if resp.status_code >= 400:
        error = data.get("error") or {}
        raise ApiError(resp.status_code, error.get("message") or str(data)[:300])
    return data


def stream_ask(question: str, top_k: int, mode: str):
    """调用 /ask/stream，逐条产出 SSE 事件（dict）。

    为什么用 httpx.stream 而不是 requests：
    requests 要把整个响应收完才能读；stream 会边收边给，这是流式的前提。
    """
    payload = {"question": question, "top_k": top_k, "mode": mode}
    with httpx.stream("POST", f"{API_BASE}/ask/stream", json=payload, timeout=TIMEOUT) as resp:
        if resp.status_code >= 400:
            body = resp.read().decode("utf-8", "ignore")[:300]
            raise ApiError(resp.status_code, body)
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            try:
                yield json.loads(line[len("data:"):].strip())
            except ValueError:
                continue


# ---------------- 侧边栏：连接信息与检索参数 ----------------
with st.sidebar:
    st.title("🛎️ FDE Support Copilot")
    st.caption("知识库问答 · 工单管理 · Agent 待确认操作")
    st.text_input("API 地址", value=API_BASE, key="api_base_display", disabled=True)

    try:
        health = api("GET", "/health")
        st.success(f"服务正常 · 索引 {health.get('index_count')} 条 · DB {health.get('db')}")
    except ApiError as exc:
        st.error(f"健康检查失败：{exc}")

    st.divider()
    top_k = st.slider("检索片段数", 1, 8, 4)
    mode = st.selectbox("检索模式", ["hybrid", "vector", "bm25"], index=0)
    st.caption("hybrid = 向量 + BM25 用 RRF 融合（M5）")

tab_ask, tab_tickets, tab_agent, tab_obs = st.tabs(["💬 知识问答", "🎫 工单", "🤖 Agent", "📈 观测"])


# ==================== Tab 1：流式知识问答 ====================
with tab_ask:
    st.subheader("知识库问答（流式）")

    # 对话历史放在 session_state：脚本重跑不会丢
    if "qa_history" not in st.session_state:
        st.session_state.qa_history = []

    for item in st.session_state.qa_history:
        with st.chat_message(item["role"]):
            st.markdown(item["content"])
            if item.get("citations"):
                with st.expander(f"引用 {len(item['citations'])} 处"):
                    for c in item["citations"]:
                        st.markdown(f"[{c['n']}] **{c['doc']}** / {c['section']}")
            if item.get("hits"):
                with st.expander(f"检索命中 {len(item['hits'])} 条"):
                    for h in item["hits"]:
                        st.markdown(f"`{h['n']}` {h['doc_id']} / {h['section']} · score={h['score']}")

    question = st.chat_input("问点什么，比如：红字发票怎么开")
    if question:
        st.session_state.qa_history.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)

        with st.chat_message("assistant"):
            placeholder = st.empty()
            parts: list[str] = []
            hits: list[dict] = []
            citations: list[dict] = []
            status = st.status("检索中…", expanded=False)

            try:
                for event in stream_ask(question, top_k, mode):
                    etype = event.get("type")
                    if etype == "meta":
                        hits = event.get("hits", [])
                        status.update(label=f"检索到 {len(hits)} 条，正在生成…")
                    elif etype == "delta":
                        parts.append(event.get("text", ""))
                        # 每来一段就重画：这是"打字机效果"的全部实现
                        placeholder.markdown("".join(parts) + "▌")
                    elif etype == "done":
                        citations = event.get("citations", [])
                        status.update(label="完成", state="complete")
                    elif etype == "error":
                        status.update(label="模型错误", state="error")
                        st.error(event.get("message", "模型服务异常"))
            except ApiError as exc:
                st.error(f"请求失败（{exc.status}）：{exc}")
                parts = parts or ["（没有拿到回答）"]

            answer = "".join(parts)
            placeholder.markdown(answer or "（空回答）")
            if citations:
                st.caption("引用：" + "；".join(f"[{c['n']}] {c['doc']} / {c['section']}" for c in citations))
            if hits:
                with st.expander(f"检索命中 {len(hits)} 条"):
                    for h in hits:
                        st.markdown(f"`{h['n']}` {h['doc_id']} / {h['section']} · score={h['score']}")

        st.session_state.qa_history.append(
            {"role": "assistant", "content": answer, "citations": citations, "hits": hits}
        )

    if st.session_state.qa_history and st.button("清空对话", key="clear_qa"):
        st.session_state.qa_history = []
        st.rerun()


# ==================== Tab 2：工单 ====================
with tab_tickets:
    st.subheader("工单")

    col1, col2 = st.columns([1, 3])
    with col1:
        status_filter = st.selectbox(
            "状态", ["全部", "open", "in_progress", "resolved", "closed", "escalated"]
        )
    with col2:
        customer_filter = st.text_input("客户号（可空）", placeholder="C001")

    params: dict = {}
    if status_filter != "全部":
        params["status"] = status_filter
    if customer_filter.strip():
        params["customer_id"] = customer_filter.strip()

    try:
        tickets = api("GET", "/tickets", params=params)
    except ApiError as exc:
        tickets = []
        st.error(f"读取工单失败：{exc}")

    st.caption(f"共 {len(tickets)} 张")
    if tickets:
        st.dataframe(
            [
                {
                    "id": t["id"], "客户": t["customer_id"], "标题": t["title"],
                    "状态": t["status"], "优先级": t["priority"], "创建时间": t["created_at"],
                }
                for t in tickets
            ],
            width="stretch",
            hide_index=True,
        )

        selected = st.selectbox("查看详情", [t["id"] for t in tickets])
        detail = api("GET", f"/tickets/{selected}")
        st.markdown(f"**{detail['title']}** · {detail['status']} · {detail['priority']}")
        st.write(detail["description"])
        st.markdown("事件时间线：")
        for e in detail["events"]:
            st.markdown(f"- `{e['created_at']}` **{e['event_type']}**（{e['actor']}）{e['content']}")
    else:
        st.info("没有符合条件的工单。")

    st.divider()
    left, right = st.columns(2)

    # st.form：把多个输入打包，点"提交"才触发一次重跑（否则每改一个字段就重跑）
    with left:
        st.markdown("**新建工单**")
        with st.form("create_ticket", clear_on_submit=True):
            customer_id = st.text_input("客户号", value="C001")
            title = st.text_input("标题")
            description = st.text_area("描述")
            priority = st.selectbox("优先级", ["normal", "low", "high", "urgent"])
            submitted = st.form_submit_button("创建")
        if submitted:
            try:
                created = api("POST", "/tickets", json={
                    "customer_id": customer_id, "title": title,
                    "description": description, "priority": priority,
                })
                st.success(f"已创建 {created['id']}")
                st.rerun()
            except ApiError as exc:
                st.error(f"创建失败（{exc.status}）：{exc}")

    with right:
        st.markdown("**更新状态**")
        with st.form("update_status"):
            ticket_id = st.text_input("工单号", value=tickets[0]["id"] if tickets else "")
            new_status = st.selectbox("目标状态", ["in_progress", "resolved", "closed", "escalated"])
            note = st.text_input("说明（可空）")
            updated = st.form_submit_button("更新")
        if updated:
            try:
                result = api("PATCH", f"/tickets/{ticket_id}/status",
                             json={"status": new_status, "note": note})
                st.success(f"{result['id']} → {result['status']}")
                st.rerun()
            except ApiError as exc:
                # 409 = 非法流转；400 = 参数矛盾；404 = 工单不存在
                st.error(f"更新失败（{exc.status}）：{exc}")


# ==================== Tab 3：Agent ====================
with tab_agent:
    st.subheader("Agent（写操作需确认）")
    st.caption("读操作（查订单/查工单/检索）自动执行；建单、改状态会先变成待确认单。")

    if "agent_session" not in st.session_state:
        # session_id 由客户端生成：服务端不存会话内容，只用它绑定待确认操作
        st.session_state.agent_session = str(uuid.uuid4())
    if "agent_history" not in st.session_state:
        st.session_state.agent_history = []
    if "agent_pending" not in st.session_state:
        st.session_state.agent_pending = None

    for item in st.session_state.agent_history:
        with st.chat_message(item["role"]):
            st.markdown(item["content"])
            if item.get("steps"):
                with st.expander("工具调用过程"):
                    for s in item["steps"]:
                        st.markdown(
                            f"- 第 {s['step']} 步 `{s['tool']}` · {s['action']} · "
                            f"{s['elapsed_ms']}ms · args=`{json.dumps(s['args'], ensure_ascii=False)}`"
                        )

    # 待确认单：必须让用户看清"将要做什么"，而不是只显示一个工具名
    if st.session_state.agent_pending:
        p = st.session_state.agent_pending
        st.warning(f"待确认操作：**{p['tool']}**（{p['pending_id']}，{p['expires_at']} 前有效）")
        st.code(json.dumps(p["arguments"], ensure_ascii=False, indent=2), language="json")
        c1, c2 = st.columns(2)
        if c1.button("✅ 同意执行", key="approve"):
            try:
                res = api("POST", "/agent/confirm", json={
                    "pending_id": p["pending_id"],
                    "session_id": st.session_state.agent_session,
                    "decision": "approve",
                })
                st.success(f"已执行：{res['status']}")
                st.json(res.get("result") or {})
                st.session_state.agent_pending = None
                st.rerun()
            except ApiError as exc:
                st.error(f"确认失败（{exc.status}）：{exc}")
        if c2.button("❌ 拒绝", key="reject"):
            try:
                api("POST", "/agent/confirm", json={
                    "pending_id": p["pending_id"],
                    "session_id": st.session_state.agent_session,
                    "decision": "reject",
                })
                st.info("已拒绝，未执行任何写操作。")
                st.session_state.agent_pending = None
                st.rerun()
            except ApiError as exc:
                st.error(f"拒绝失败（{exc.status}）：{exc}")

    message = st.chat_input("比如：订单 O001 收到时破了，帮我登记退款工单")
    if message:
        st.session_state.agent_history.append({"role": "user", "content": message})
        with st.chat_message("user"):
            st.markdown(message)

        with st.chat_message("assistant"):
            with st.spinner("模型思考与调用工具中…"):
                try:
                    data = api("POST", "/agent/chat", json={
                        "session_id": st.session_state.agent_session,
                        "message": message,
                    })
                except ApiError as exc:
                    data = {}
                    st.error(f"Agent 请求失败（{exc.status}）：{exc}")

            answer = data.get("answer") or ""
            steps = data.get("steps") or []
            pending = data.get("pending")

            if answer:
                st.markdown(answer)
            if steps:
                with st.expander(f"工具调用过程（{len(steps)} 步）"):
                    for s in steps:
                        st.markdown(
                            f"- 第 {s['step']} 步 `{s['tool']}` · {s['action']} · "
                            f"{s['elapsed_ms']}ms · args=`{json.dumps(s['args'], ensure_ascii=False)}`"
                        )
            if pending:
                st.warning("模型提议了一个写操作，已暂停，等待你确认（见上方确认单）。")
                st.session_state.agent_pending = pending

        st.session_state.agent_history.append(
            {"role": "assistant", "content": answer or "（等待确认）", "steps": steps}
        )
        if pending:
            st.rerun()

    if st.button("清空 Agent 对话", key="clear_agent"):
        st.session_state.agent_history = []
        st.session_state.agent_pending = None
        st.rerun()


# ==================== Tab 4：观测（M9） ====================
# 这一页只读 /traces 和 /metrics/summary。
# 为什么把它放前端而不是只看数据库：排查问题时想看"刚才那次调用发生了什么"，
# 打开网页点两下比写 SQL 快得多。
with tab_obs:
    st.subheader("调用观测")
    st.caption("trace 只存摘要（命中、引用数、耗时、状态），不存答案全文。")

    if st.button("刷新", key="refresh_obs"):
        st.rerun()

    try:
        summary = api("GET", "/metrics/summary")
    except ApiError as exc:
        summary = {}
        st.error(f"读取指标失败：{exc}")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("总调用数", summary.get("count", 0))
    c2.metric("p50 延迟", f"{summary.get('latency_p50_ms') or '-'} ms")
    c3.metric("p95 延迟", f"{summary.get('latency_p95_ms') or '-'} ms")
    c4.metric("状态分布", ", ".join(f"{k}:{v}" for k, v in (summary.get("by_status") or {}).items()) or "-")

    if (summary.get("count") or 0) < 20:
        st.caption("样本少于 20 条时 p95 意义有限，先多跑几次再看。")

    st.markdown("**按类型分布**：" + (", ".join(
        f"{k}: {v}" for k, v in (summary.get("by_kind") or {}).items()) or "-"))

    kind = st.selectbox("筛选类型", ["全部", "ask", "ask_stream", "agent_chat", "agent_confirm"])
    params = {"limit": 50}
    if kind != "全部":
        params["kind"] = kind

    try:
        traces = api("GET", "/traces", params=params)
    except ApiError as exc:
        traces = []
        st.error(f"读取 trace 失败：{exc}")

    if traces:
        st.dataframe(
            [
                {
                    "id": t["id"], "类型": t["kind"], "状态": t["status"],
                    "耗时ms": t["latency_ms"], "步数": t["steps"],
                    "query": (t["query"] or "")[:40], "时间": t["created_at"],
                }
                for t in traces
            ],
            width="stretch",
            hide_index=True,
        )
        selected_id = st.selectbox("查看详情", [t["id"] for t in traces])
        try:
            st.json(api("GET", f"/traces/{selected_id}"))
        except ApiError as exc:
            st.error(f"读取详情失败：{exc}")
    else:
        st.info("还没有调用记录。去问一个问题，或者跑一次 Agent。")
