"""M11 附加能力：Agent 跨轮历史 + 流式接口异步并发。

两个测试都刻意不联网：
- 历史：打桩 loop.chat，只看 messages 结构与会话表
- 并发：打桩 achat_stream（异步生成器 + sleep），用 ASGITransport 并发打 /ask/stream
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

import app.agent.loop as loop
import app.api.main as api
from app.db import repositories as repo
from app.db.connection import get_connection, init_db


@pytest.fixture()
def agent_db(tmp_path):
    path = tmp_path / "agent.db"
    init_db(path)
    conn = get_connection(path)
    with conn:
        conn.execute(
            "INSERT INTO customers(id,name,phone,email,level,created_at) "
            "VALUES ('C001','张三','','','vip','2026-01-01T00:00:00+00:00')"
        )
    yield conn, path
    conn.close()


def _fake_hits() -> list[dict]:
    return [{
        "text": "片段", "doc_id": "发票与报销", "title": "发票与报销",
        "section": "## 4. 红字发票", "chunk_index": 0,
        "source_path": "发票与报销.md", "score": 0.6,
    }]


# ---------------- 跨轮历史 ----------------

def test_history_is_passed_to_next_turn(agent_db, monkeypatch):
    conn, _ = agent_db
    captured: list[list[dict]] = []

    def fake_chat(messages, tools=None):
        captured.append(messages)
        return {"content": "订单 O001 已付款", "tool_calls": []}

    monkeypatch.setattr(loop, "chat", fake_chat)

    loop.run_agent(conn, session_id="s1", message="订单 O001 怎么样")
    loop.run_agent(conn, session_id="s1", message="那帮我退了吧")

    # 第二轮的 messages：system → 第一轮 user → 第一轮 assistant → 本轮 user
    second = captured[1]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user"]
    assert "订单 O001" in second[1]["content"]
    assert "已付款" in second[2]["content"]

    # 会话表里应有完整两轮
    rows = repo.list_agent_messages(conn, "s1")
    assert [r["role"] for r in rows] == ["user", "assistant", "user", "assistant"]


def test_history_respects_char_budget(agent_db, monkeypatch):
    conn, _ = agent_db
    monkeypatch.setattr(loop, "chat", lambda m, tools=None: {"content": "答案" * 50, "tool_calls": []})
    loop.run_agent(conn, session_id="s2", message="第一轮")

    # 把字符上限压到很小：第二轮应该带不上任何历史
    monkeypatch.setattr(loop, "HISTORY_MAX_CHARS", 10)
    captured: list[list[dict]] = []
    monkeypatch.setattr(loop, "chat",
                        lambda m, tools=None: captured.append(m) or {"content": "ok", "tool_calls": []})
    loop.run_agent(conn, session_id="s2", message="第二轮")

    assert [m["role"] for m in captured[0]] == ["system", "user"]


def test_pending_is_recorded_as_history_note(agent_db, monkeypatch):
    """写操作提议也要留一句话，否则下一轮完全不知道刚才提过单。"""
    conn, _ = agent_db
    monkeypatch.setattr(loop, "chat", lambda m, tools=None: {
        "content": "",
        "tool_calls": [{"id": "c1", "name": "create_ticket",
                        "arguments": {"customer_id": "C001", "title": "t", "description": "d"}}],
    })
    result = loop.run_agent(conn, session_id="s3", message="帮我登记")
    assert result["pending"] is not None

    rows = repo.list_agent_messages(conn, "s3")
    assert rows[-1]["role"] == "assistant"
    assert result["pending"]["pending_id"] in rows[-1]["content"]


# ---------------- 异步并发 ----------------

def test_stream_handles_concurrent_requests(monkeypatch):
    """/ask/stream 改成 async 后，8 个并发请求的耗时应该接近 1 个，而不是 8 个。"""
    monkeypatch.setattr(api, "load_settings", lambda: None)
    monkeypatch.setattr(api, "is_relevant", lambda q: (True, 0.0))
    monkeypatch.setattr(api, "search_hybrid", lambda q, **kw: _fake_hits())
    monkeypatch.setattr(api, "build_messages", lambda q, hits, **kw: [{"role": "user", "content": q}])
    monkeypatch.setattr(api, "record_trace_detached", lambda **kw: None)

    async def fake_achat_stream(messages, **kwargs):
        for _ in range(3):
            await asyncio.sleep(0.1)      # 模拟"等模型"
            yield "片"

    monkeypatch.setattr(api, "achat_stream", fake_achat_stream)

    async def scenario() -> tuple[list[int], float]:
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async def one() -> int:
                async with client.stream("POST", "/ask/stream", json={"question": "q"}) as resp:
                    async for _ in resp.aiter_lines():
                        pass
                    return resp.status_code

            started = time.perf_counter()
            codes = await asyncio.gather(*[one() for _ in range(8)])
            return codes, time.perf_counter() - started

    codes, elapsed = asyncio.run(scenario())
    assert codes == [200] * 8
    # 串行需要 8 × 0.3s ≈ 2.4s；并发应接近 0.3s。留 1.2s 余量防慢机器抖动。
    assert elapsed < 1.2, f"并发没有生效：{elapsed:.2f}s"


def test_reset_endpoint_clears_history(agent_db, monkeypatch):
    conn, path = agent_db
    repo.add_agent_message(conn, session_id="sess4", role="user", content="hello")

    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(db_path=str(path)))
    client = TestClient(api.app, raise_server_exceptions=False)

    resp = client.post("/agent/reset", json={"session_id": "sess4"})
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 1
    assert repo.list_agent_messages(conn, "sess4") == []
