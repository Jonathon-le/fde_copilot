"""核心回归测试。

三条原则：
1. **不联网、不调模型**：需要模型的地方 monkeypatch。
2. **不碰 data/app.db**：每个测试用 tmp_path 建独立库。
3. **测行为，不测实现**：断言"不会建出两张单"，而不是"某函数被调用了几次"。

运行：
    python -m pytest -q
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api.main as api
from app.agent.confirm import confirm_pending
from app.db import repositories as repo
from app.db.connection import get_connection, init_db
from app.rag.hybrid import rrf_fuse
from app.rag.tokenizer import tokenize


# ---------------- 夹具 ----------------

@pytest.fixture()
def db(tmp_path):
    """独立数据库 + 最小种子数据。返回 (conn, path)。"""
    path = tmp_path / "test.db"
    init_db(path)
    conn = get_connection(path)
    with conn:
        conn.execute(
            "INSERT INTO customers(id,name,phone,email,level,created_at) "
            "VALUES ('C001','张三','','','vip','2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO orders(id,customer_id,status,amount,tracking_no,carrier,created_at) "
            "VALUES ('O001','C001','paid',100.0,'SF1','顺丰','2026-01-01T00:00:00+00:00')"
        )
    yield conn, path
    conn.close()


def _fake_hit(doc_id: str, chunk_index: int) -> dict:
    """和 search_vector 同构的 7 键命中，用于纯函数测试。"""
    return {
        "text": "片段", "doc_id": doc_id, "title": doc_id,
        "section": "## 1. 测试", "chunk_index": chunk_index,
        "source_path": f"{doc_id}.md", "score": 0.9,
    }


# ---------------- 纯函数 ----------------

def test_tokenize_is_stable_and_normalized():
    """建索引与查询必须走同一个分词结果，大小写/标点不能造成 token 分叉。"""
    a = tokenize("退款多久到账？")
    b = tokenize("退款多久到账？")
    assert a == b
    assert all(t == t.lower() for t in a)
    assert "？" not in a and "?" not in a


def test_rrf_dedup_and_order():
    """同一 chunk 在两路都出现时只保留一条，分数相加。"""
    fused = rrf_fuse(
        [[_fake_hit("A", 0), _fake_hit("B", 1)],
         [_fake_hit("A", 0)]],
        top_k=4,
    )
    keys = [f"{h['doc_id']}::{h['chunk_index']}" for h in fused]
    assert len(keys) == len(set(keys)) == 2
    assert keys[0] == "A::0"                       # 两路都有，排第一
    assert fused[0]["score"] == pytest.approx(1 / 61 + 1 / 61)


# ---------------- 领域规则 ----------------

def test_closed_ticket_cannot_reopen(db):
    conn, _ = db
    with conn:
        conn.execute(
            "INSERT INTO tickets(id,customer_id,title,description,status,priority,created_at,updated_at) "
            "VALUES ('TKT-1','C001','t','d','closed','normal','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
        )
    with pytest.raises(repo.ValidationError):
        repo.update_ticket_status(conn, "TKT-1", new_status="open", actor="test", note="")


def test_pending_approve_is_idempotent(db):
    """连点两次"同意"只能建一张工单——M7 最核心的不变量。"""
    conn, _ = db
    args = {"customer_id": "C001", "title": "破损退款", "description": "外包装破损"}
    pending_id = repo.create_pending(
        conn, session_id="s1", tool_name="create_ticket", arguments=args
    )["id"]

    first = confirm_pending(conn, pending_id=pending_id, session_id="s1", decision="approve")
    second = confirm_pending(conn, pending_id=pending_id, session_id="s1", decision="approve")

    assert first["status"] == "executed"
    assert second["status"] == "executed"
    assert second["result"] == first["result"]
    count = conn.execute("SELECT COUNT(*) AS n FROM tickets").fetchone()["n"]
    assert count == 1


def test_confirm_with_wrong_session_is_404(db):
    conn, _ = db
    pid = repo.create_pending(
        conn, session_id="owner", tool_name="create_ticket",
        arguments={"customer_id": "C001", "title": "t", "description": "d"},
    )["id"]
    with pytest.raises(repo.NotFoundError):
        confirm_pending(conn, pending_id=pid, session_id="intruder", decision="approve")


# ---------------- HTTP 层 ----------------

@pytest.fixture()
def client(db, monkeypatch):
    conn, path = db
    # get_db 在请求时调用模块级 get_settings；替换它即可让 API 指向临时库。
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(db_path=str(path)))
    return TestClient(api.app, raise_server_exceptions=False), path


def test_api_validation_and_404(client):
    c, _ = client
    assert c.post("/ask", json={"question": ""}).status_code == 422
    assert c.post("/ask", json={"question": "x", "mode": "foo"}).status_code == 422
    assert c.get("/tickets/TKT-NOPE").status_code == 404


def test_ask_returns_citations_and_writes_trace(client, monkeypatch):
    c, path = client
    monkeypatch.setattr(api, "is_relevant", lambda q: (True, 0.0))
    monkeypatch.setattr(api, "search_hybrid", lambda q, **kw: [_fake_hit("发票与报销", 0)])
    monkeypatch.setattr(api, "chat", lambda messages, **kw: {"content": "按政策处理 [1]", "tool_calls": []})

    resp = c.post("/ask", json={"question": "红字发票怎么开"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["citations"][0]["doc"] == "发票与报销"

    conn = get_connection(path)
    try:
        trace = conn.execute("SELECT * FROM traces").fetchone()
        assert trace is not None and trace["kind"] == "ask" and trace["status"] == "ok"
    finally:
        conn.close()


def test_trace_endpoints(client):
    c, _ = client
    assert c.get("/traces").status_code == 200
    assert c.get("/traces?kind=ask").json() == []
    assert c.get("/traces/999").status_code == 404
    summary = c.get("/metrics/summary").json()
    assert summary["count"] == 0 and summary["latency_p50_ms"] is None
