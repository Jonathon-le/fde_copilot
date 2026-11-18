"""半自动演示：对运行中的 API 跑一遍关键链路。

用法：
    python -m scripts.demo_walkthrough
    python -m scripts.demo_walkthrough --base-url http://127.0.0.1:8001
    python -m scripts.demo_walkthrough --skip-agent   # 只跑不花钱的步骤

前提：API 已启动（python -m uvicorn app.api.main:app）。
脚本会调用两次模型（/ask 与 /agent/chat 各一次），其余步骤不花钱。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

OK, FAIL = "[OK]", "[FAIL]"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="demo_walkthrough", description="演示脚本")
    parser.add_argument("--base-url", default=os.getenv("API_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--ask-question", default="红字发票怎么开")
    parser.add_argument("--agent-message", default="订单 O001 现在什么状态")
    parser.add_argument("--skip-ask", action="store_true", help="跳过 /ask（省一次模型调用）")
    parser.add_argument("--skip-agent", action="store_true", help="跳过 /agent/chat")
    return parser


def step(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{OK if ok else FAIL} {name}" + (f" —— {detail}" if detail else ""))
    return ok


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = args.base_url.rstrip("/")
    session = "demo-" + str(int(time.time()))
    client = httpx.Client(base_url=base, timeout=180.0, trust_env=False)

    print(f"目标 API: {base}\n")

    # 0) 连通性
    try:
        health = client.get("/health").json()
    except httpx.RequestError:
        print(f"{FAIL} 连不上 {base}。先启动：python -m uvicorn app.api.main:app")
        return 2
    if not step("健康检查", health.get("status") == "ok",
                f"db={health.get('db')} index={health.get('index_count')}"):
        return 1

    # 1) 知识库问答
    if not args.skip_ask:
        started = time.perf_counter()
        resp = client.post("/ask", json={"question": args.ask_question, "top_k": 4})
        elapsed = time.perf_counter() - started
        body = resp.json() if resp.content else {}
        ok = resp.status_code == 200 and bool(body.get("answer"))
        step("POST /ask", ok,
             f"{resp.status_code} · {elapsed:.1f}s · citations={len(body.get('citations', []))} · "
             f"answer={str(body.get('answer', ''))[:40]}…")

    # 2) Agent：读工具
    if not args.skip_agent:
        started = time.perf_counter()
        resp = client.post("/agent/chat", json={"session_id": session, "message": args.agent_message})
        elapsed = time.perf_counter() - started
        body = resp.json() if resp.content else {}
        steps = body.get("steps") or []
        tools = [(s.get("tool"), s.get("action")) for s in steps]
        ok = resp.status_code == 200 and bool(steps)
        step("POST /agent/chat", ok, f"{resp.status_code} · {elapsed:.1f}s · steps={tools}")

        pending = body.get("pending")
        if pending:
            step("产生待确认操作", True,
                 f"{pending['tool']} · {pending['pending_id']} · {pending['expires_at']}")
            # 演示用"拒绝"：能验证状态机，又不往库里写东西
            confirm = client.post("/agent/confirm", json={
                "pending_id": pending["pending_id"],
                "session_id": session,
                "decision": "reject",
            })
            step("POST /agent/confirm (reject)", confirm.status_code == 200,
                 f"{confirm.status_code} · {confirm.json().get('status')}")
            again = client.post("/agent/confirm", json={
                "pending_id": pending["pending_id"],
                "session_id": session,
                "decision": "reject",
            })
            step("重复 reject（幂等）", again.status_code == 200,
                 f"{again.status_code} · {again.json().get('status')}")

    # 3) 观测
    summary = client.get("/metrics/summary").json()
    step("GET /metrics/summary", True,
         f"count={summary.get('count')} p50={summary.get('latency_p50_ms')}ms "
         f"p95={summary.get('latency_p95_ms')}ms")

    traces = client.get("/traces", params={"limit": 5}).json()
    step("GET /traces", isinstance(traces, list), f"{len(traces)} 条")
    for t in traces[:3]:
        print(f"     #{t['id']} {t['kind']:13} {t['status']:9} {t['latency_ms']}ms "
              f"{json.dumps(json.loads(t['payload']) if t['payload'] else {}, ensure_ascii=False)[:60]}")

    print("\n演示完成。界面：http://127.0.0.1:8501 （python -m streamlit run app/ui/streamlit_app.py）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
