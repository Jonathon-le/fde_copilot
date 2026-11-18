"""工单命令行工具。

用法示例：

    python -m scripts.ticket_cli init-db
    python -m scripts.ticket_cli seed
    python -m scripts.ticket_cli list
    python -m scripts.ticket_cli list --status open
    python -m scripts.ticket_cli show TKT-1001
    python -m scripts.ticket_cli create --customer C001 --order O001 \
        --title "退款申请" --description "商品破损，申请退款" --priority high
    python -m scripts.ticket_cli update TKT-1001 --status resolved \
        --actor agent --note "已电话联系客户"
"""

import argparse
import sqlite3
import sys
import unicodedata

from app.config import get_settings
from app.db.connection import get_connection, init_db
from app.db.repositories import (
    TICKET_PRIORITIES,
    TICKET_STATUSES,
    NotFoundError,
    ValidationError,
    create_ticket,
    get_ticket,
    list_ticket_events,
    list_tickets,
    update_ticket_status,
)
from app.db.seed import seed_data

# list 命令的列名和显示宽度（中文占两格，所以要按显示宽度补空格）。
LIST_COLUMNS = (
    ("ID", 12),
    ("客户", 6),
    ("标题", 24),
    ("状态", 12),
    ("优先级", 9),
    ("更新时间", 26),
)


def display_width(text: str) -> int:
    """返回字符串在终端里占的格数，中日韩全角字符按两格算。"""
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        for ch in text
    )


def pad(text: str, width: int) -> str:
    """按显示宽度右侧补空格，不按字符数补。"""
    return text + " " * max(0, width - display_width(text))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="工单 CLI 工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="初始化数据库建表，不插入种子数据")
    subparsers.add_parser("seed", help="建表并写入种子数据，可重复执行")

    list_parser = subparsers.add_parser("list", help="列出工单")
    list_parser.add_argument(
        "--status", choices=sorted(TICKET_STATUSES), help="按状态筛选"
    )
    list_parser.add_argument("--customer", help="按客户 ID 筛选")

    show_parser = subparsers.add_parser("show", help="查看工单详情和事件时间线")
    show_parser.add_argument("ticket_id", help="工单 ID")

    create_parser = subparsers.add_parser("create", help="创建工单")
    create_parser.add_argument("--customer", required=True, help="客户 ID")
    create_parser.add_argument("--order", help="订单 ID，可以不填")
    create_parser.add_argument("--title", required=True, help="工单标题")
    create_parser.add_argument("--description", required=True, help="问题描述")
    create_parser.add_argument(
        "--priority",
        default="normal",
        choices=sorted(TICKET_PRIORITIES),
        help="优先级，默认 normal",
    )

    update_parser = subparsers.add_parser("update", help="更新工单状态")
    update_parser.add_argument("ticket_id", help="工单 ID")
    update_parser.add_argument(
        "--status", required=True, choices=sorted(TICKET_STATUSES), help="新状态"
    )
    update_parser.add_argument("--actor", required=True, help="操作人")
    update_parser.add_argument("--note", required=True, help="变更说明，会写进事件")

    return parser


def cmd_init_db(db_path) -> int:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(db_path)
    print(f"数据库初始化完成：{db_path}")
    return 0


def cmd_seed(db_path) -> int:
    # 任务卡要求 seed 先建表再插数据；init_db 本身可重复执行。
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(db_path)
    conn = get_connection(db_path)
    try:
        seed_data(conn)
    finally:
        conn.close()
    print("种子数据导入完成")
    return 0


def cmd_list(conn: sqlite3.Connection, args) -> int:
    tickets = list_tickets(conn, status=args.status, customer_id=args.customer)
    if not tickets:
        print("没有符合条件的工单")
        return 0

    header = "".join(pad(name, width) for name, width in LIST_COLUMNS).rstrip()
    print(header)
    print("-" * display_width(header))

    for ticket in tickets:
        values = (
            ticket["id"],
            ticket["customer_id"],
            ticket["title"],
            ticket["status"],
            ticket["priority"],
            ticket["updated_at"],
        )
        line = "".join(
            pad(value, width)
            for value, (_, width) in zip(values, LIST_COLUMNS)
        )
        print(line.rstrip())
    return 0


def cmd_show(conn: sqlite3.Connection, args) -> int:
    ticket = get_ticket(conn, args.ticket_id)
    if ticket is None:
        print(f"错误：工单 {args.ticket_id} 不存在", file=sys.stderr)
        return 1

    events = list_ticket_events(conn, ticket["id"])

    print("===== 工单详情 =====")
    print(f"ID:       {ticket['id']}")
    print(f"客户:     {ticket['customer_id']}")
    print(f"订单:     {ticket['order_id'] or '-'}")
    print(f"标题:     {ticket['title']}")
    print(f"描述:     {ticket['description']}")
    print(f"状态:     {ticket['status']}")
    print(f"优先级:   {ticket['priority']}")
    print(f"创建时间: {ticket['created_at']}")
    print(f"更新时间: {ticket['updated_at']}")
    print()
    print("===== 事件时间线 =====")
    for event in events:
        print(
            f"[{event['created_at']}] "
            f"{event['event_type']:<14} {event['actor']:<10} {event['content']}"
        )
    return 0


def cmd_create(conn: sqlite3.Connection, args) -> int:
    ticket = create_ticket(
        conn,
        customer_id=args.customer,
        title=args.title,
        description=args.description,
        order_id=args.order,
        priority=args.priority,
    )
    print(ticket["id"])
    return 0


def cmd_update(conn: sqlite3.Connection, args) -> int:
    ticket = update_ticket_status(
        conn,
        ticket_id=args.ticket_id,
        new_status=args.status,
        actor=args.actor,
        note=args.note,
    )
    print(f"更新成功：{ticket['id']} -> {ticket['status']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = get_settings().db_path

    # init-db / seed 自己管理连接，不需要在这里开。
    if args.command == "init-db":
        return cmd_init_db(db_path)
    if args.command == "seed":
        return cmd_seed(db_path)

    # 其余命令都是只读或写业务数据，统一开一个连接，最后在 finally 关闭。
    if not db_path.exists():
        print(
            f"错误：数据库 {db_path} 不存在，请先运行 init-db 和 seed",
            file=sys.stderr,
        )
        return 1

    conn = get_connection(db_path)
    try:
        if args.command == "list":
            return cmd_list(conn, args)
        if args.command == "show":
            return cmd_show(conn, args)
        if args.command == "create":
            return cmd_create(conn, args)
        return cmd_update(conn, args)
    except (NotFoundError, ValidationError) as exc:
        # 业务错误：CLI 负责翻译成 stderr + 退出码 1，不甩 traceback。
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except sqlite3.OperationalError as exc:
        print(f"数据库错误：{exc}", file=sys.stderr)
        print("如果表还没建，请先运行 python -m scripts.ticket_cli init-db", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
