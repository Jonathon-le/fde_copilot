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

