"""Agent 循环：让模型自己决定调哪个工具，直到它能给出答案。

一次 run_agent 的完整往返：

    system + user
        ↓ chat(tools=...)
    assistant（带 tool_calls）          ← 模型说"我要调 get_order"
        ↓ 执行工具（读工具直接执行）
    tool（结果 JSON）
        ↓ chat(tools=...) 再来一轮
    assistant（无 tool_calls）          ← 模型给出最终回答，循环结束

本模块只负责"循环"和"读工具"；写工具不在这里执行，只登记成待确认操作。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time

from app.agent.tools import READ_TOOLS, WRITE_TOOLS, execute_read, schema_for_model
from app.db import repositories as repo
from app.llm.client import chat

logger = logging.getLogger(__name__)

# 系统提示词就是"行为契约"：它决定了模型会不会滥用工具、会不会编单号。
SYSTEM_PROMPT = """你是企业的客服助手。规则：
1. 需要订单、客户、工单信息时调用工具，不要凭空编造单号或状态。
2. 需要政策、流程、规则时先检索知识库，引用检索到的内容作答。
3. 需要创建工单或修改工单状态时，直接调用对应工具；系统会先让用户确认，你不用自己问"要不要确认"。
4. 回答简洁，用中文；不要向用户展示工具名或内部字段。
5. 信息不足时直接说明缺什么，不要猜。"""

# 单个工具结果最多塞回多少字符。上下文是有限的，订单/检索结果全量回灌会挤掉对话本身。
_MAX_TOOL_RESULT_CHARS = 2000

# 一个会话最多堆多少个待确认操作。防模型连环提议，把确认页刷爆。
MAX_PENDING_PER_SESSION = 5


def _tool_message_payload(result: dict) -> str:
    """工具结果 -> 回灌给模型的字符串。

    OpenAI 协议要求 tool 消息的 content 是**字符串**，不是对象。
    超长就截断：模型只需要关键字段，不需要全量。
    """
    text = json.dumps(result, ensure_ascii=False)
    if len(text) > _MAX_TOOL_RESULT_CHARS:
        text = text[:_MAX_TOOL_RESULT_CHARS] + "…(已截断)"
    return text


def _assistant_message(content: str, tool_calls: list[dict]) -> dict:
    """把模型这一轮的回复原样放回 messages。

    两个必须遵守的协议细节：
    1. assistant 消息要带 tool_calls，否则下一轮的 tool 消息没有对应关系，服务端会 400；
    2. arguments 必须是 JSON **字符串**，而 _parse_tool_calls 给我们的是 dict，
       所以要重新 json.dumps。
    """
    return {
        "role": "assistant",
        "content": content or "",
        "tool_calls": [
            {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                },
            }
            for tc in tool_calls
        ],
    }


def _pending_payload(row: dict) -> dict:
    """数据库行 -> 对外的 pending 结构。

    为什么不直接返回数据库行：里面没必要暴露 status/created_at 等内部列，
    也不会误把 tool_name 之类的列名变化带到前端契约里。
    """
    return {
        "pending_id": row["id"],
        "tool": row["tool_name"],
        "arguments": json.loads(row["arguments"]),
        "expires_at": repo.pending_expires_at(row["created_at"]),
    }


def run_agent(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    message: str,
    max_steps: int = 5,
) -> dict:
    """跑一轮 Agent 对话。

    返回 {"answer": str, "steps": list, "pending": dict | None}：
    - answer：给用户的回答（没拿到时是兜底话术）
    - steps ：每一步调了哪个工具、成功与否、耗时（M9 的 trace 雏形）
    - pending：有写操作等待确认时非空；此时 answer 可能为空
    """
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": message},
    ]
    # 读工具和写工具都要给模型看：模型得知道"可以建单"，才会提议建单。
    # 但"能不能建"不取决于它看到了什么，而取决于确认流程（见 confirm.py）。
    tools = schema_for_model({**READ_TOOLS, **WRITE_TOOLS})
    steps: list[dict] = []
    seen: dict[str, int] = {}      # 同一工具+同一参数出现了几次，用于防死循环
    last_answer = ""

    # for ... else：循环跑满 max_steps 还没 break，就走 else 里的兜底话术。
    for step in range(1, max_steps + 1):
        result = chat(messages, tools=tools)
        tool_calls = result["tool_calls"]

        if not tool_calls:
            # 模型不再调工具 = 它认为自己能回答了
            last_answer = result["content"].strip()
            break

        # 先把这一轮 assistant 回复放回历史，再逐个执行工具
        messages.append(_assistant_message(result["content"], tool_calls))

        for tc in tool_calls:
            name = tc["name"]
            args = tc["arguments"]
            started = time.perf_counter()

            # --- 1) 重复检测 ---
            # 放在最前面：连未知工具反复调用也要能尽早停，否则会一路烧到 max_steps。
            key = name + "::" + json.dumps(args, ensure_ascii=False, sort_keys=True)
            seen[key] = seen.get(key, 0) + 1
            if seen[key] >= 2:
                steps.append({
                    "step": step, "tool": name, "args": args, "ok": False,
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "action": "repeat_abort",
                })
                return {
                    "answer": "同一个查询已经重复执行，我先停下。请补充更具体的信息。",
                    "steps": steps,
                    "pending": None,
                }

            # --- 2) 写工具：不执行，落成待确认操作，本轮结束 ---
            # 这是整个 M7 的安全边界：模型只能"提议"，不能"落库"。
            if name in WRITE_TOOLS:
                if repo.count_pending(conn, session_id) >= MAX_PENDING_PER_SESSION:
                    return {
                        "answer": "待确认的操作太多了，请先处理已有确认单。",
                        "steps": steps,
                        "pending": None,
                    }
                row = repo.create_pending(
                    conn, session_id=session_id, tool_name=name, arguments=args
                )
                steps.append({
                    "step": step, "tool": name, "args": args, "ok": True,
                    "elapsed_ms": 0, "action": "pending",
                })
                return {
                    "answer": last_answer,
                    "steps": steps,
                    "pending": _pending_payload(row),
                }

            # --- 3) 读工具：白名单内的直接执行 ---
            if name not in READ_TOOLS:
                # 模型可能编出不存在/不该暴露的工具名。回灌 ok:false 让它自己纠正，
                # 而不是抛异常把整个会话打断。
                tool_result = {"ok": False, "reason": "unknown_tool",
                               "error": f"工具 {name} 不存在"}
            else:
                tool_result = execute_read(name, args, conn)

            steps.append({
                "step": step, "tool": name, "args": args,
                "ok": tool_result.get("ok", False),
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "action": "executed",
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": _tool_message_payload(tool_result),
            })
    else:
        last_answer = "这个问题需要的步骤过多，我先停下。请补充更具体的信息（比如订单号）。"

    return {"answer": last_answer, "steps": steps, "pending": None}
