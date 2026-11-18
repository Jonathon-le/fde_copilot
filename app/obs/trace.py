"""埋点工具：把一次调用的摘要写进 traces 表。

两条使用路径：
- record_trace(conn, ...)         请求内使用（复用 Depends(get_db) 的连接）
- record_trace_detached(...)      流式响应用：自己开短连接

为什么流式要单独一个函数：
FastAPI 的 yield 依赖在路由**返回时**就关闭连接，而流式响应在返回之后才真正生成。
在生成器里用请求级连接写库，会拿到 "Cannot operate on a closed database"。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager

from app.config import get_settings
from app.db import repositories as repo
from app.db.connection import get_connection

logger = logging.getLogger(__name__)


def record_trace(conn: sqlite3.Connection, *, kind: str, status: str,
                 latency_ms: int, session_id: str | None = None,
                 query: str | None = None, steps: int | None = None,
                 payload: dict | None = None) -> None:
    """写一条 trace。**失败只记日志，绝不影响主请求**：
    观测系统本身不该成为新的故障点。
    """
    try:
        repo.create_trace(conn, kind=kind, status=status, latency_ms=latency_ms,
                          session_id=session_id, query=query, steps=steps, payload=payload)
    except Exception:
        logger.exception("record trace failed: %s", kind)


def record_trace_detached(**kwargs) -> None:
    """独立连接版：给流式响应/后台收尾用。"""
    try:
        conn = get_connection(get_settings().db_path)
        try:
            record_trace(conn, **kwargs)
        finally:
            conn.close()
    except Exception:
        logger.exception("record detached trace failed: %s", kwargs.get("kind"))


@contextmanager
def timed():
    """计时上下文：with timed() as t: ... ; t["ms"] 是耗时。

    用 perf_counter（单调时钟）：time.time() 会被系统校时/NTP 调整影响，
    可能算出负数或跳变的耗时。
    """
    started = time.perf_counter()
    box = {"ms": 0}
    try:
        yield box
    finally:
        box["ms"] = int((time.perf_counter() - started) * 1000)
