import secrets
import sqlite3
from datetime import datetime, timezone

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
