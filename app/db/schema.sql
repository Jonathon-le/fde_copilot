CREATE TABLE if NOT EXISTS customers
(id TEXT PRIMARY KEY,
name TEXT NOT NULL,
phone TEXT,
email TEXT,
level TEXT NOT NULL DEFAULT 'normal' CHECK(level in('normal','vip','svip')), 
created_at TEXT NOT NULL
);
CREATE TABLE if not EXISTS orders
(id TEXT PRIMARY KEY ,
customer_id TEXT NOT NULL ,
status TEXT NOT NULL CHECK(status in('pending','paid','shipped','delivered','refunded')),
amount REAL NOT NULL CHECK(amount>=0),
tracking_no TEXT,
carrier TEXT,
created_at TEXT NOT NULL,
FOREIGN KEY(customer_id)REFERENCES customers(id) ON DELETE NO ACTION
);
CREATE TABLE IF NOT EXISTS tickets
(id TEXT PRIMARY KEY,
customer_id TEXT NOT NULL,
order_id TEXT,
title TEXT NOT NULL,
description TEXT NOT NULL,
status TEXT NOT NULL DEFAULT 'open' CHECK(status in('open','in_progress','resolved','closed','escalated')),
priority TEXT NOT NULL DEFAULT 'normal' CHECK(priority in('low','normal','high','urgent')),
created_at TEXT NOT NULL,
updated_at TEXT NOT NULL,
FOREIGN KEY(customer_id)REFERENCES customers(id) ON DELETE NO ACTION,
FOREIGN KEY(order_id)REFERENCES orders(id)
);
CREATE TABLE IF NOT EXISTS ticket_events
(id INTEGER PRIMARY KEY AUTOINCREMENT,
ticket_id TEXT NOT NULL,
event_type TEXT NOT NULL CHECK(event_type in('created','status_changed','note','escalated')),
content TEXT NOT NULL,
actor TEXT NOT NULL DEFAULT 'system',
created_at TEXT NOT NULL,
FOREIGN KEY(ticket_id)REFERENCES tickets(id)
);
-- tickets(customer_id)
CREATE INDEX IF NOT EXISTS idx_tickets_customer_id ON tickets(customer_id);
-- tickets(status)
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
-- ticket_events(ticket_id)
CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket_id ON ticket_events(ticket_id);
-- orders(customer_id)
CREATE INDEX IF NOT EXISTS idx_orders_customer_id ON orders(customer_id);


-- 待确认的写操作（Agent 提议 → 人工确认 → 执行）
-- 为什么落库而不是放内存：--reload 重启、多 worker、崩溃都会丢内存里的待办，
-- 而"用户已经看到确认单、点确认却 404"是不可接受的。
-- 注意：session_id 是客户端生成的会话标识，不对应 customers 表，所以没有外键。
CREATE TABLE IF NOT EXISTS pending_actions(
    id TEXT PRIMARY KEY,                 -- PA-XXXXXXXX
    session_id TEXT NOT NULL,            -- 谁的会话（确认时校验，防越权）
    tool_name TEXT NOT NULL,             -- 要执行的写工具名
    arguments TEXT NOT NULL,             -- 工具参数，JSON 字符串
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status in('pending','confirmed','executed','failed','rejected','expired')),
    created_at TEXT NOT NULL,            -- UTC ISO；是否超时用它判断
    decided_at TEXT,                     -- 用户确认/拒绝的时间
    result TEXT                          -- 执行结果 JSON（重复确认时原样返回）
);
-- 按会话+状态查：确认页要列出待办，限流也要用它
CREATE INDEX IF NOT EXISTS idx_pending_session ON pending_actions(session_id, status);
