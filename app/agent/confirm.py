"""确认执行：把"模型提议的写操作"变成"真正落库的写操作"。

这是整个 Agent 链路里唯一拥有写权限的入口。所有安全检查都在这里：

    pending ──approve──> confirmed ──成功──> executed
       │                    └────失败──> failed
       ├──reject──> rejected
       └──超时────> expired

三条不变量（改代码时不要破坏）：
1. 只有 pending 能被执行；其他状态要么幂等返回，要么 409。
2. 同一个 pending_id 重复 approve，返回第一次的结果，**不重复执行**。
3. session_id 不匹配一律 404——不能让人靠状态码探测"这个 id 是否存在"。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from app.agent.tools import WRITE_TOOLS, execute_write
from app.db import repositories as repo

logger = logging.getLogger(__name__)


def _is_expired(created_at: str) -> bool:
    """确认单是否超时。

    时间一律用 UTC 比较：库里的 created_at 由 _now_utc() 写入，带时区；
    老数据可能没有时区（naive），统一补成 UTC 再比。
    """
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created > timedelta(seconds=repo.PENDING_TTL_SECONDS)


def _out(row: dict, status: str, result: dict | None = None) -> dict:
    return {"pending_id": row["id"], "status": status, "result": result}


def _note(conn: sqlite3.Connection, row: dict, text: str) -> None:
    """把确认结果写进 Agent 会话历史，供下一轮对话使用。

    只在状态真正发生变化时调用（幂等重放不写），否则历史会被重复条目刷满。
    """
    try:
        repo.add_agent_message(conn, session_id=row["session_id"],
                               role="assistant", content=text)
    except Exception:
        logger.exception("save confirm history failed")


def confirm_pending(
    conn: sqlite3.Connection,
    *,
    pending_id: str,
    session_id: str,
    decision: str,
) -> dict:
    """处理一次确认。

    decision: "approve"（执行）| "reject"（拒绝）
    返回 {"pending_id", "status", "result"}；非法/越权/超时抛领域异常，
    由 API 层的 errors.py 统一翻译成 404 / 409。
    """
    row = repo.get_pending(conn, pending_id)

    # 不存在和"不是你的"返回同一个 404：
    # 如果越权返回 403，攻击者就能用一个 id 试出"它存在"。
    if row is None or row["session_id"] != session_id:
        raise repo.NotFoundError(f"确认单 {pending_id} 不存在")

    status = row["status"]

    # ---------------- 拒绝 ----------------
    if decision == "reject":
        if status == "rejected":
            return _out(row, "rejected")        # 幂等：重复拒绝不算错
        if status != "pending":
            raise repo.ConflictError(f"当前状态 {status} 不能拒绝")
        if _is_expired(row["created_at"]):
            repo.mark_pending_decided(conn, pending_id, "expired")
            raise repo.ConflictError("确认单已超时，请重新发起")
        repo.mark_pending_decided(conn, pending_id, "rejected")
        _note(conn, row, f"用户拒绝了待确认操作 {row['tool_name']}（{row['id']}），未执行任何写操作。")
        return _out(row, "rejected")

    # ---------------- 同意 ----------------
    # 幂等：已经执行过，原样返回第一次的结果，绝不再执行一次。
    if status == "executed":
        return _out(row, "executed", json.loads(row["result"]) if row["result"] else None)

    # 已拒绝 / 已超时 / 已失败：都不能再执行。
    # failed 也不能重试——"失败→重试→成功"会让副作用发生两次，
    # 正确做法是让用户重新发起一次新的提议。
    if status == "rejected":
        raise repo.ConflictError("该操作已被拒绝")
    if status == "expired":
        raise repo.ConflictError("确认单已超时，请重新发起")
    if status == "failed":
        raise repo.ConflictError("上次执行失败，不能重复确认，请重新发起")
    if status == "confirmed":
        # 用户在我们执行完之前又点了一次，或上次进程在执行中途崩了。
        # 保守处理：拒绝，让人去查，而不是冒险再执行一遍。
        raise repo.ConflictError("该操作正在执行或上次执行中断，请稍后查询结果")

    # 到这里 status 一定是 pending
    if _is_expired(row["created_at"]):
        repo.mark_pending_decided(conn, pending_id, "expired")
        raise repo.ConflictError("确认单已超时，请重新发起")

    tool_name = row["tool_name"]
    if tool_name not in WRITE_TOOLS:
        # 防御性检查：即使数据库被手工改过，也不能执行非白名单工具。
        raise repo.ValidationError(f"工具 {tool_name} 不是可执行的写操作")

    arguments = json.loads(row["arguments"])

    # 先落 confirmed 再执行：万一执行到一半进程挂了，状态停在 confirmed，
    # 上面那条分支会拒绝重放，而不是悄悄再执行一次。
    repo.mark_pending_decided(conn, pending_id, "confirmed")

    try:
        result = execute_write(tool_name, arguments, conn)
    except Exception:
        # execute_write 内部已经把业务异常转成 ok:false，走到这里说明是 bug。
        # 记日志、落 failed，别让确认单永远卡在 confirmed。
        logger.exception("confirm execute failed: %s", tool_name)
        repo.set_pending_result(conn, pending_id, "failed",
                                {"ok": False, "error": "执行失败，请联系管理员"})
        _note(conn, row, f"待确认操作 {row['tool_name']}（{row['id']}）执行失败。")
        return _out(row, "failed", {"ok": False, "error": "执行失败，请联系管理员"})

    final_status = "executed" if result.get("ok") else "failed"
    repo.set_pending_result(conn, pending_id, final_status, result)
    if final_status == "executed":
        ticket = (result or {}).get("ticket") or {}
        detail = f"，工单 {ticket['id']}" if ticket.get("id") else ""
        _note(conn, row, f"待确认操作 {row['tool_name']}（{row['id']}）已执行成功{detail}。")
    else:
        _note(conn, row, f"待确认操作 {row['tool_name']}（{row['id']}）执行失败："
                         f"{(result or {}).get('error', '未知原因')}")
    return _out(row, final_status, result)
