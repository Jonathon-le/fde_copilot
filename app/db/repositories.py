import json
import math
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

TICKET_STATUSES = {"open", "in_progress", "resolved", "closed", "escalated"}
TICKET_PRIORITIES = {"low", "normal", "high", "urgent"}





class NotFoundError(Exception):
    """要查询或修改的数据不存在。"""


class ValidationError(Exception):
    """调用方传入的参数不合法。"""


class ConflictError(ValidationError):
    """请求本身合法，但和资源当前状态冲突（如非法状态流转）。

    继承 ValidationError：CLI 里老的 `except ValidationError` 仍然能接住它，
    不用改 ticket_cli.py。
    """

def _now_utc() -> str:
    """统一使用 UTC 的 ISO 时间，避免本地时区导致排序和比较混乱。"""
    return datetime.now(timezone.utc).isoformat()


def get_customer(conn: sqlite3.Connection, customer_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM customers WHERE id=?",
        (customer_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def get_order(conn: sqlite3.Connection, order_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM orders WHERE id=?",
        (order_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def list_tickets(
    conn: sqlite3.Connection,
    status: str | None = None,
    customer_id: str | None = None,
) -> list[dict]:
    conditions = []
    params = []

    if status is not None:
        conditions.append("status=?")
        params.append(status)
    if customer_id is not None:
        conditions.append("customer_id=?")
        params.append(customer_id)

    sql = "SELECT * FROM tickets"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY created_at DESC, id ASC"

    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def get_ticket(conn: sqlite3.Connection, ticket_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM tickets WHERE id=?",
        (ticket_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def list_ticket_events(
    conn: sqlite3.Connection,
    ticket_id: str,
) -> list[dict]:
    # 按自增 id 排序，保证同一秒创建的事件也有稳定顺序。
    rows = conn.execute(
        "SELECT * FROM ticket_events WHERE ticket_id=? ORDER BY id ASC",
        (ticket_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def create_ticket(
    conn: sqlite3.Connection,
    customer_id: str,
    title: str,
    description: str,
    order_id: str | None = None,
    priority: str = "normal",
    actor: str = "cli",
) -> dict:
    if priority not in TICKET_PRIORITIES:
        raise ValidationError(f"无效的优先级：{priority}")
    if not title or not title.strip():
        raise ValidationError("工单标题不能为空")
    if not description or not description.strip():
        raise ValidationError("工单描述不能为空")

    ticket_id = f"TKT-{secrets.token_hex(4).upper()}"
    now = _now_utc()

    # with conn 会在正常退出时提交，在异常退出时回滚。
    with conn:
        cur = conn.cursor()

        # 客户必须存在。
        cur.execute("SELECT 1 FROM customers WHERE id=?", (customer_id,))
        if cur.fetchone() is None:
            raise NotFoundError(f"客户 {customer_id} 不存在")

        # 如果指定了订单，订单必须存在，并且属于当前客户。
        if order_id is not None:
            cur.execute(
                "SELECT 1 FROM orders WHERE id=? AND customer_id=?",
                (order_id, customer_id),
            )
            if cur.fetchone() is None:
                raise ValidationError(
                    f"订单 {order_id} 不存在，或者不属于客户 {customer_id}"
                )

        cur.execute(
            """
            INSERT INTO tickets
                (id, customer_id, title, description, order_id,
                 priority, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticket_id,
                customer_id,
                title,
                description,
                order_id,
                priority,
                "open",
                now,
                now,
            ),
        )

        # 每张工单创建时都写入一条事件，方便后续展示时间线。
        cur.execute(
            """
            INSERT INTO ticket_events
                (ticket_id, event_type, actor, content, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ticket_id, "created", actor, "工单创建", now),
        )

        # 在提交前读出完整记录；如果这里失败，整个事务会回滚。
        row = cur.execute(
            "SELECT * FROM tickets WHERE id=?",
            (ticket_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"工单 {ticket_id} 创建后查询失败")
        return dict(row)

_ALLOWED_TRANSITIONS = {
    "open":        {"in_progress", "resolved", "escalated", "closed"},
    "in_progress": {"resolved", "escalated", "closed"},
    "escalated":   {"in_progress", "resolved", "closed"},
    "resolved":    {"in_progress", "closed"},
    "closed":      set(),
}

def update_ticket_status(
    conn: sqlite3.Connection,
    ticket_id: str,
    new_status: str,
    actor: str,
    note: str,
) -> dict:
    if new_status not in TICKET_STATUSES:
        raise ValidationError(f"无效的工单状态：{new_status}")

    now = _now_utc()

    with conn:
        cur = conn.cursor()

        # 工单必须存在。
        cur.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,))
        row=cur.fetchone()
        if row is None:
            raise NotFoundError(f"工单 {ticket_id} 不存在")
        current = row["status"]
        if new_status != current and new_status not in _ALLOWED_TRANSITIONS[current]:
                    raise ConflictError(f"工单不能从 {current} 变为 {new_status}")
        cur.execute(
            """
            UPDATE tickets
            SET status=?, updated_at=?
            WHERE id=?
            """,
            (new_status, now, ticket_id),
        )

        # 状态变化也要留下事件记录。
        cur.execute(
            """
            INSERT INTO ticket_events
                (ticket_id, event_type, actor, content, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ticket_id, "status_changed", actor, note, now),
        )

        row = cur.execute(
            "SELECT * FROM tickets WHERE id=?",
            (ticket_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"工单 {ticket_id} 更新后查询失败")
        return dict(row)


# ==================== 待确认操作（pending_actions） ====================
# 状态机：
#   pending --approve--> confirmed --成功--> executed
#      |                     \------失败--> failed
#      |--reject--> rejected
#      \--超时----> expired
# 只有 pending 能被确认；其余状态再确认要么幂等返回，要么 409。

PENDING_TTL_SECONDS = 30 * 60   # 确认单有效期：30 分钟


def pending_expires_at(created_at: str) -> str:
    """由 created_at 算出到期时间。不单独存列：TTL 以后要调，存了就得多写一次迁移。"""
    created = datetime.fromisoformat(created_at)
    return (created + timedelta(seconds=PENDING_TTL_SECONDS)).isoformat()


def create_pending(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    tool_name: str,
    arguments: dict,
) -> dict:
    """把一次写操作提议存成待确认记录，返回整行。"""
    pending_id = f"PA-{secrets.token_hex(4).upper()}"
    now = _now_utc()
    with conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO pending_actions
                (id, session_id, tool_name, arguments, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (pending_id, session_id, tool_name,
             json.dumps(arguments, ensure_ascii=False), now),
        )
        row = cur.execute(
            "SELECT * FROM pending_actions WHERE id=?", (pending_id,)
        ).fetchone()
    if row is None:
        raise RuntimeError(f"待确认操作 {pending_id} 创建后查询失败")
    return dict(row)


def get_pending(conn: sqlite3.Connection, pending_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM pending_actions WHERE id=?", (pending_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def count_pending(conn: sqlite3.Connection, session_id: str) -> int:
    """该会话里还没处理的待办数量，用于限流（防止模型刷单）。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM pending_actions WHERE session_id=? AND status='pending'",
        (session_id,),
    ).fetchone()
    return int(row["n"])


def mark_pending_decided(conn: sqlite3.Connection, pending_id: str, status: str) -> None:
    """记录用户的决定：confirmed / rejected / expired。"""
    if status not in ("confirmed", "rejected", "expired"):
        raise ValidationError(f"无效的待确认状态：{status}")
    with conn:
        conn.execute(
            "UPDATE pending_actions SET status=?, decided_at=? WHERE id=?",
            (status, _now_utc(), pending_id),
        )


def set_pending_result(
    conn: sqlite3.Connection,
    pending_id: str,
    status: str,
    result: dict | None,
) -> None:
    """记录执行结果：executed / failed。result 存 JSON，幂等重放时原样返回。"""
    if status not in ("executed", "failed"):
        raise ValidationError(f"无效的执行结果状态：{status}")
    with conn:
        conn.execute(
            "UPDATE pending_actions SET status=?, result=? WHERE id=?",
            (status, json.dumps(result, ensure_ascii=False) if result is not None else None,
             pending_id),
        )


# ==================== 观测：traces ====================
# 原则：只存摘要（命中、引用数、错误类型），不存答案全文、不存 header/key。
# trace 是诊断材料，不是备份；存全文会让表迅速膨胀，也扩大泄露面。

TRACE_KINDS = ("ask", "ask_stream", "agent_chat", "agent_confirm")


def create_trace(
    conn: sqlite3.Connection,
    *,
    kind: str,
    status: str,
    latency_ms: int,
    session_id: str | None = None,
    query: str | None = None,
    steps: int | None = None,
    payload: dict | None = None,
) -> int:
    if kind not in TRACE_KINDS:
        raise ValidationError(f"无效的 trace 类型：{kind}")
    with conn:
        cur = conn.execute(
            """
            INSERT INTO traces (kind, session_id, query, status, latency_ms, steps, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (kind, session_id, query, status, latency_ms, steps,
             json.dumps(payload, ensure_ascii=False) if payload else None,
             _now_utc()),
        )
        return int(cur.lastrowid)


def list_traces(conn: sqlite3.Connection, *, limit: int = 50,
                kind: str | None = None) -> list[dict]:
    sql = "SELECT * FROM traces"
    params: list = []
    if kind is not None:
        sql += " WHERE kind=?"
        params.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_trace(conn: sqlite3.Connection, trace_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM traces WHERE id=?", (trace_id,)).fetchone()
    return dict(row) if row is not None else None


def trace_stats(conn: sqlite3.Connection) -> dict:
    """汇总指标。分位数在 Python 里算：SQLite 没有 percentile 函数，34 条数据也不值得上扩展。"""
    rows = conn.execute("SELECT kind, status, latency_ms FROM traces").fetchall()
    latencies = sorted(int(r["latency_ms"]) for r in rows)

    def pct(p: float) -> int | None:
        if not latencies:
            return None
        # 最近秩法：ceil(p/100 * n) - 1
        idx = max(0, min(len(latencies) - 1, math.ceil(p / 100 * len(latencies)) - 1))
        return latencies[idx]

    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1

    return {
        "count": len(rows),
        "by_kind": by_kind,
        "by_status": by_status,
        "latency_p50_ms": pct(50),
        "latency_p95_ms": pct(95),
    }
