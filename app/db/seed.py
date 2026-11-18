"""工单库的种子数据。

三条设计约束：

1. 主键全部固定，重复执行不会插入第二份；
2. 外键顺序必须是 customers -> orders -> tickets -> ticket_events；
3. 整个函数放在一个事务里，中途失败不会留下半成品数据。
"""

import sqlite3

# 固定时间戳：两次运行的输出完全一致，验收时可以直接对比。
SEED_CREATED_AT = "2026-11-01T09:00:00+00:00"
SEED_UPDATED_AT = "2026-11-01T10:30:00+00:00"

# 每行元组对应下面 INSERT 的列顺序，改列名时两边要一起改。
CUSTOMERS = [
    ("C001", "张三", "13800000001", "zhangsan@example.com", "vip"),
    ("C002", "李四", "13800000002", "lisi@example.com", "normal"),
    ("C003", "王五", "13800000003", "wangwu@example.com", "normal"),
]

ORDERS = [
    ("O001", "C001", "paid", 299.00, "SF1234567890", "顺丰"),
    ("O002", "C002", "shipped", 89.50, "YT9876543210", "圆通"),
    ("O003", "C003", "delivered", 1299.00, "JD0001112223", "京东"),
]

TICKETS = [
    ("TKT-1001", "C001", "O001", "退款申请",
     "商品外包装破损，申请全额退款", "open", "high"),
    ("TKT-1002", "C002", "O002", "物流轨迹长时间未更新",
     "订单显示已发货，但三天没有物流轨迹", "in_progress", "normal"),
    ("TKT-1003", "C003", "O003", "发票抬头开错",
     "需要把发票抬头改为公司名称并重开", "resolved", "low"),
]

# 事件表主键是自增整数，不由我们控制，所以不能靠 INSERT OR IGNORE 去重，
# 下面会用“先查再插”处理。
TICKET_EVENTS = [
    ("TKT-1001", "created", "工单创建：客户提交退款申请", "customer", SEED_CREATED_AT),
    ("TKT-1002", "created", "工单创建：客户反馈物流轨迹未更新", "customer", SEED_CREATED_AT),
    ("TKT-1002", "status_changed", "客服已接单，正在联系承运方核实", "agent", SEED_UPDATED_AT),
    ("TKT-1003", "created", "工单创建：客户申请重开发票", "customer", SEED_CREATED_AT),
    ("TKT-1003", "status_changed", "发票已重开并邮件发送给客户", "agent", SEED_UPDATED_AT),
]


def seed_data(conn: sqlite3.Connection) -> None:
    """写入固定种子数据，可重复执行。"""
    # 整个 seed 一个事务：任何一步失败，前面的插入全部回滚。
    with conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO customers
                (id, name, phone, email, level, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (customer_id, name, phone, email, level, SEED_CREATED_AT)
                for customer_id, name, phone, email, level in CUSTOMERS
            ],
        )

        conn.executemany(
            """
            INSERT OR IGNORE INTO orders
                (id, customer_id, status, amount, tracking_no, carrier, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (order_id, customer_id, status, amount, tracking_no, carrier,
                 SEED_CREATED_AT)
                for order_id, customer_id, status, amount, tracking_no, carrier
                in ORDERS
            ],
        )

        conn.executemany(
            """
            INSERT OR IGNORE INTO tickets
                (id, customer_id, order_id, title, description,
                 status, priority, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    ticket_id, customer_id, order_id, title, description,
                    status, priority,
                    SEED_CREATED_AT,
                    # 已经是处理中/已解决的工单，更新时间晚于创建时间。
                    SEED_CREATED_AT if status == "open" else SEED_UPDATED_AT,
                )
                for ticket_id, customer_id, order_id, title, description, status, priority
                in TICKETS
            ],
        )

        # 事件主键自增，OR IGNORE 挡不住重复；用业务字段组合判断是否已存在。
        for ticket_id, event_type, content, actor, created_at in TICKET_EVENTS:
            exists = conn.execute(
                """
                SELECT 1 FROM ticket_events
                WHERE ticket_id=? AND event_type=? AND content=?
                """,
                (ticket_id, event_type, content),
            ).fetchone()
            if exists is None:
                conn.execute(
                    """
                    INSERT INTO ticket_events
                        (ticket_id, event_type, content, actor, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (ticket_id, event_type, content, actor, created_at),
                )
