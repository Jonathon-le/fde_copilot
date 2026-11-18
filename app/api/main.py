import json
import sqlite3
import re
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from app.api.errors import register_error_handlers
from app.agent.confirm import confirm_pending
from app.agent.loop import run_agent
from app.api.schemas import (
    AgentChatRequest, AgentChatResponse, AgentConfirmRequest, AgentConfirmResponse, AgentResetRequest,
    AskRequest, AskResponse, Citation, HitOut,
    TicketCreate, TicketDetail, TicketOut, StatusUpdate, TicketStatus,
)
from app.config import get_settings
from app.db.connection import get_connection, init_db
from app.llm.client import LLMError, achat_stream, chat, chat_stream, load_settings
from app.obs.trace import record_trace, record_trace_detached
from app.rag.relevance import is_relevant
from app.rag.hybrid import search_hybrid
from app.rag.index import IndexEmptyError, get_collection
from app.rag.prompt import build_messages

# 用模块别名 repo 导入，而不是 from ... import list_tickets：
# 否则仓库函数和路由函数同名，一眼看不出调的是哪个。
from app.db import repositories as repo

@asynccontextmanager
async def lifespan(app:FastAPI) :
    settings=get_settings()
    settings.ensure_dirs()
    init_db(settings.db_path)
    yield
app=FastAPI(
    title="FDE Support Copilot API",
    version="0.1.0",
    description="企业知识库+工单服务",
    lifespan=lifespan,
)
register_error_handlers(app)
def get_db() :
    conn=get_connection(get_settings().db_path)
    try :
        yield conn
    finally :
        conn.close()
@app.get("/health",summary="健康检查",description="返回服务、数据库、向量库的基础状态",
         responses={200:{"content":{"application/json":{"example":{"status":"ok","db":"ok","index_count":34}}}}})
def health(conn:sqlite3.Connection=Depends(get_db))->dict :
    db_status="ok"
        #Depends()自动执行传入的函数并且拿到它yield或return的结果
          #检查数据库的基础状态
    try :
            conn.execute("SELECT 1").fetchone()
    except sqlite3.Error as exc :
         #返回服务的基础状态
            db_status=f"error:{type(exc).__name__}"
    #检查向量库的基础状态
    try :
        index_count=get_collection().count()
    except Exception :
        index_count=None
    return{"status":"ok","db":db_status,"index_count":index_count}

CITATION_RE=re.compile(r"\[(\d+)\]")#匹配[数字];括号捕获里面的数字
NO_CONTEXT_ANSWER="资料中没有提到这个问题。"

def _extract_citations(content:str,hits:list[dict]) ->list[Citation] :
     seen:list[int]=[]
     #遍历所有[数字]匹配结果
     for match in CITATION_RE.finditer(content):
          #group(1)拿到捕获的数字
          n=int(match.group(1))
          if n not in seen :
               seen.append(n)#去重,同一个引用只保留一次
     out=[]
     for n in seen :
          #校验n必须合法,不能超过召回hits的总数
          if 1<=n<=len(hits) :
               hit=hits[n-1] #引用是从1开始,数组是从0开始
               out.append(
                    Citation(
                         n=n,
                         doc=hit["doc_id"],
                         section=hit["section"].lstrip("#").strip(),
                             )
               )
     return out
@app.post("/ask",response_model=AskResponse,summary="知识库问答",description="检索知识库并让模型基于片段作答并给出引用；检索结果为空或低于相关性门槛时返回固定话术，不调用模型。")
def ask(req:AskRequest,conn:sqlite3.Connection=Depends(get_db))->AskResponse :
     started=time.perf_counter()
     status="ok"
     payload={}
     try :
          #0)相关性门槛（默认关闭，见 app/rag/relevance.py 的说明）
          relevant,top_score=is_relevant(req.question)
          if not relevant :
               status="refused"
               payload={"gate":{"top_score":round(top_score,4)}}
               return AskResponse(answer=NO_CONTEXT_ANSWER,citations=[],hits=[])

          #1)检索。向量库为空时 search_hybrid 抛 IndexEmptyError，
          #  由 errors.py 的全局处理器统一变成 503，这里不再 try/except
          hits=search_hybrid(req.question,top_k=req.top_k,mode=req.mode)
          hit_out=[
               HitOut(
                    n=i,
                    doc_id=h["doc_id"],
                    section=h["section"].lstrip("#").strip(),
                    score=round(h["score"],6),
               )
               for i,h in enumerate(hits,start=1)
          ]
          #2)挡在模型前面：没命中就不花钱，不幻觉
          #只在索引为空时发生
          if not hits :
               status="refused"
               return AskResponse(answer=NO_CONTEXT_ANSWER,citations=[],hits=[])
          #3)只有有上下文时才调用模型。LLM 失败由全局处理器变成 502
          content=chat(build_messages(req.question,hits))["content"]
          citations=_extract_citations(content,hits)
          payload={"hits":len(hits),"top_doc":hits[0]["doc_id"],"citations":len(citations)}
          return AskResponse(answer=content,citations=citations,hits=hit_out)
     except Exception as exc :
          status="error"
          # 只记异常类型名：异常原文可能带上游 URL / 响应片段
          payload={"error":type(exc).__name__}
          raise
     finally :
          record_trace(conn,kind="ask",status=status,
                       latency_ms=int((time.perf_counter()-started)*1000),
                       query=req.question,payload=payload or None)

@app.get("/tickets", response_model=list[TicketOut], summary="工单列表",  description="支持按状态和客户过滤，参数不传则不过滤；按创建时间倒序。")
def list_tickets_api(
    # Query(default=None) 表示"不传就不过滤"；不写默认值的话 FastAPI 会当成必填。
    # status 用 TicketStatus 而不是 str，拼错值直接 422。
    status: TicketStatus | None = Query(default=None, description="按状态过滤", examples=["open"]),
    customer_id: str | None = Query(default=None, max_length=32, description="按客户过滤", examples=["C001"]),
    # 连接由依赖注入，请求结束时自动关闭，这里只负责用。
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    # 直接返回仓库给的 dict 列表，字段过滤交给 response_model。
    # 不要在路由里手写 dict 拼装：那等于把契约写了两遍。
    return repo.list_tickets(conn, status=status, customer_id=customer_id)


@app.get("/tickets/{ticket_id}", response_model=TicketDetail, summary="工单详情",description="返回工单本身和完整事件时间线；工单不存在返回 404。")
def get_ticket_api(
    ticket_id: Annotated[str, Path(description="工单编号，如 TKT-1001", examples=["TKT-1001"])],
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    ticket = repo.get_ticket(conn, ticket_id)
    # 这一步不能省。仓库查不到返回 None，如果直接 return None，
    # response_model=TicketDetail 校验失败 → 500，而不是 404。
    if ticket is None:
        # 抛领域异常而不是 HTTPException：状态码的映射统一由 errors.py 负责，
        # 路由层不出现 HTTP 概念
        raise repo.NotFoundError(f"工单 {ticket_id} 不存在")

    # 详情比列表多一个 events。用 {**ticket, "events": ...} 平铺，
    # 客户端拿到的结构是"工单字段 + events"，少一层解包。
    events = repo.list_ticket_events(conn, ticket_id)
    return {**ticket, "events": events}


# status_code=201 表示"已创建"。
# 创建必须是 201 而不是 200：200 分不清"查到了"和"建成了"，
# 客户端就没法靠状态码判断该不该去 Location 取资源。
@app.post("/tickets", response_model=TicketOut, status_code=201, summary="创建工单",description="创建后状态固定为 open，actor 固定为 api；响应头 Location 指向新工单。")
def create_ticket_api(
    req: TicketCreate,
    # 用 Response 参数是为了能写响应头；直接返回 JSONResponse 也行，
    # 但那样会绕过 response_model 的字段过滤。
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    # 客户不存在（NotFoundError → 404）、订单不属于该客户（ValidationError → 400）
    # 都由全局处理器翻译，这里不需要 try/except
    ticket = repo.create_ticket(
        conn,
        customer_id=req.customer_id,
        title=req.title,
        description=req.description,
        order_id=req.order_id,
        priority=req.priority,
        actor="api",       # 审计字段服务端写死，客户端传什么都不会生效
    )

    # Location 指向新资源的地址，客户端不需要再猜 URL 怎么拼。
    response.headers["Location"] = f"/tickets/{ticket['id']}"
    return ticket


@app.patch("/tickets/{ticket_id}/status", response_model=TicketOut, summary="更新工单状态",description="按状态流转规则更新；非法流转返回 409，工单不存在返回 404。")
def update_ticket_status_api(
    ticket_id: Annotated[str, Path(description="工单编号，如 TKT-1001", examples=["TKT-1001"])],
    req: StatusUpdate,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    # 客户端没写说明时给个默认文案：事件表里不能出现空白 content。
    note = req.note or f"状态更新为 {req.status}"
    # 工单不存在 → 404（NotFoundError）
    # 非法流转 → 409（repository 抛的是 ConflictError，它是 ValidationError 的子类，
    # errors.py 里单独注册了它，所以不会被 ValidationError 的 400 抢走）
    return repo.update_ticket_status(
        conn, ticket_id, new_status=req.status, actor="api", note=note
    )


# ==================== Agent 接口 ====================
# 和 /ask 的区别：/ask 是固定流水线（检索→作答）；
# 这里是模型自己决定调什么工具，而且写操作要经过 /agent/confirm 二次确认。

@app.post(
    "/agent/chat",
    response_model=AgentChatResponse,
    summary="客服 Agent 对话",
    description=(
        "让模型决定调用哪些工具（查订单、查工单、检索知识库）。"
        "如果它提议创建工单或改状态，不会立刻执行，而是返回 pending 等待 /agent/confirm。"
        "**注意：这个接口会多轮调用模型，耗时可能到数秒，客户端超时建议 60s 以上。**"
    ),
)
def agent_chat(
    req: AgentChatRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    # 路由只做三件事：接参数、调编排、返回。检索/工具/循环都在 agent 层。
    started = time.perf_counter()
    status = "ok"
    step_count: int | None = None
    payload: dict = {}
    try:
        result = run_agent(conn, session_id=req.session_id, message=req.message)
        steps = result.get("steps") or []
        step_count = len(steps)
        # 工具名列表进 payload：排查时一眼看出模型走了哪条路径
        payload["tools"] = [s["tool"] for s in steps]
        if result.get("pending"):
            status = "pending"
            payload["pending_tool"] = result["pending"]["tool"]
        return result
    except Exception as exc:
        status = "error"
        payload["error"] = type(exc).__name__
        raise
    finally:
        record_trace(conn, kind="agent_chat", status=status,
                     latency_ms=int((time.perf_counter() - started) * 1000),
                     session_id=req.session_id, query=req.message,
                     steps=step_count, payload=payload or None)


@app.post(
    "/agent/confirm",
    response_model=AgentConfirmResponse,
    summary="确认或拒绝待执行操作",
    description=(
        "对 /agent/chat 返回的 pending_id 做 approve / reject。"
        "重复 approve 同一 pending_id 是幂等的：返回第一次的结果，不会重复建单。"
        "确认单超过 30 分钟返回 409；session_id 不匹配返回 404。"
    ),
)
def agent_confirm(
    req: AgentConfirmRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    # 领域异常（NotFoundError/ConflictError）交给 errors.py 统一翻译成 404/409，
    # 这里不写 try/except——和 M6 的约定一致。
    started = time.perf_counter()
    status = "ok"
    payload: dict = {"decision": req.decision}
    try:
        result = confirm_pending(
            conn,
            pending_id=req.pending_id,
            session_id=req.session_id,
            decision=req.decision,
        )
        status = result.get("status", "ok")
        return result
    except Exception as exc:
        status = "error"
        payload["error"] = type(exc).__name__
        raise
    finally:
        record_trace(conn, kind="agent_confirm", status=status,
                     latency_ms=int((time.perf_counter() - started) * 1000),
                     session_id=req.session_id, payload=payload)


# ==================== 流式问答（M8） ====================
# 为什么单独一个接口而不是给 /ask 加 ?stream=true：
# 返回类型从 JSON 变成 SSE 流，客户端解析方式完全不同，混在一起两边都难写。

def _sse(payload: dict) -> str:
    """把 dict 编码成一条 SSE 事件。

    SSE 格式很简单：`data: <一行文本>\n\n`，空行表示事件结束。
    我们只在 data 里放一个 JSON，前端统一 json.loads 即可，
    不需要用 event: 字段区分类型（type 字段在 JSON 里）。
    """
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


@app.post(
    "/ask/stream",
    summary="流式知识库问答",
    description=(
        "与 `/ask` 逻辑相同，但用 SSE 逐字返回。事件类型："
        "`meta`（命中片段）→ 若干 `delta`（文本片段）→ `done`（引用）或 `error`。"
        "客户端按行读取，遇到空行结束一条事件。"
    ),
)
async def ask_stream(req: AskRequest) -> StreamingResponse:
    # 配置校验是纯内存操作，放在事件循环里没问题，而且必须"提前失败"。
    load_settings()

    # 检索是 CPU 密集（jieba/BM25/向量打分）：必须丢线程池。
    # 直接在 async 路由里同步调用，会把整个事件循环卡住——比同步路由更糟。
    relevant, top_score = await run_in_threadpool(is_relevant, req.question)
    try:
        hits = await run_in_threadpool(
            search_hybrid, req.question, top_k=req.top_k, mode=req.mode
        )
    except IndexEmptyError as exc:          # 此时还没发响应头，可以正常变 503
        raise HTTPException(status_code=503, detail="向量库为空，请先构建索引") from exc

    hit_meta = [
        {"n": i, "doc_id": h["doc_id"],
         "section": h["section"].lstrip("#").strip(), "score": round(h["score"], 6)}
        for i, h in enumerate(hits, start=1)
    ]

    async def event_stream():
        started = time.perf_counter()
        status = "ok"
        payload: dict = {"hits": len(hits)}
        if top_score:
            payload["gate"] = {"top_score": round(top_score, 4)}
        try:
            yield _sse({"type": "meta", "hits": hit_meta})

            if not relevant or not hits:
                status = "refused"
                yield _sse({"type": "delta", "text": NO_CONTEXT_ANSWER})
                yield _sse({"type": "done", "citations": []})
                return

            parts: list[str] = []
            try:
                # async for：等待模型输出时不占用线程，事件循环可以去处理别的请求
                async for piece in achat_stream(build_messages(req.question, hits)):
                    parts.append(piece)
                    yield _sse({"type": "delta", "text": piece})
            except LLMError:
                status = "error"
                yield _sse({"type": "error", "message": "模型服务暂时不可用，请稍后重试"})
                return

            content = "".join(parts)
            citations = _extract_citations(content, hits)
            payload["citations"] = len(citations)
            yield _sse({"type": "done", "citations": [c.model_dump() for c in citations]})
        finally:
            # 写库是同步 IO，同样丢线程池；且流式响应在路由返回后才结束，
            # 请求级连接已关闭，必须用独立连接。
            await run_in_threadpool(
                record_trace_detached,
                kind="ask_stream", status=status,
                latency_ms=int((time.perf_counter() - started) * 1000),
                query=req.question, payload=payload,
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ==================== Agent 接口 ====================
# 和 /ask 的区别：/ask 是固定流水线（检索→作答）；
# 这里是模型自己决定调什么工具，而且写操作要经过 /agent/confirm 二次确认。

@app.post(
    "/agent/chat",
    response_model=AgentChatResponse,
    summary="客服 Agent 对话",
    description=(
        "让模型决定调用哪些工具（查订单、查工单、检索知识库）。"
        "如果它提议创建工单或改状态，不会立刻执行，而是返回 pending 等待 /agent/confirm。"
        "**注意：这个接口会多轮调用模型，耗时可能到数秒，客户端超时建议 60s 以上。**"
    ),
)
def agent_chat(
    req: AgentChatRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    # 路由只做三件事：接参数、调编排、返回。检索/工具/循环都在 agent 层。
    started = time.perf_counter()
    status = "ok"
    step_count: int | None = None
    payload: dict = {}
    try:
        result = run_agent(conn, session_id=req.session_id, message=req.message)
        steps = result.get("steps") or []
        step_count = len(steps)
        # 工具名列表进 payload：排查时一眼看出模型走了哪条路径
        payload["tools"] = [s["tool"] for s in steps]
        if result.get("pending"):
            status = "pending"
            payload["pending_tool"] = result["pending"]["tool"]
        return result
    except Exception as exc:
        status = "error"
        payload["error"] = type(exc).__name__
        raise
    finally:
        record_trace(conn, kind="agent_chat", status=status,
                     latency_ms=int((time.perf_counter() - started) * 1000),
                     session_id=req.session_id, query=req.message,
                     steps=step_count, payload=payload or None)


@app.post(
    "/agent/confirm",
    response_model=AgentConfirmResponse,
    summary="确认或拒绝待执行操作",
    description=(
        "对 /agent/chat 返回的 pending_id 做 approve / reject。"
        "重复 approve 同一 pending_id 是幂等的：返回第一次的结果，不会重复建单。"
        "确认单超过 30 分钟返回 409；session_id 不匹配返回 404。"
    ),
)
def agent_confirm(
    req: AgentConfirmRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    # 领域异常（NotFoundError/ConflictError）交给 errors.py 统一翻译成 404/409，
    # 这里不写 try/except——和 M6 的约定一致。
    started = time.perf_counter()
    status = "ok"
    payload: dict = {"decision": req.decision}
    try:
        result = confirm_pending(
            conn,
            pending_id=req.pending_id,
            session_id=req.session_id,
            decision=req.decision,
        )
        status = result.get("status", "ok")
        return result
    except Exception as exc:
        status = "error"
        payload["error"] = type(exc).__name__
        raise
    finally:
        record_trace(conn, kind="agent_confirm", status=status,
                     latency_ms=int((time.perf_counter() - started) * 1000),
                     session_id=req.session_id, payload=payload)


# ==================== 流式问答（M8） ====================
# 为什么单独一个接口而不是给 /ask 加 ?stream=true：
# 返回类型从 JSON 变成 SSE 流，客户端解析方式完全不同，混在一起两边都难写。

def _sse(payload: dict) -> str:
    """把 dict 编码成一条 SSE 事件。

    SSE 格式很简单：`data: <一行文本>\n\n`，空行表示事件结束。
    我们只在 data 里放一个 JSON，前端统一 json.loads 即可，
    不需要用 event: 字段区分类型（type 字段在 JSON 里）。
    """
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


@app.post(
    "/ask/stream",
    summary="流式知识库问答",
    description=(
        "与 `/ask` 逻辑相同，但用 SSE 逐字返回。事件类型："
        "`meta`（命中片段）→ 若干 `delta`（文本片段）→ `done`（引用）或 `error`。"
        "客户端按行读取，遇到空行结束一条事件。"
    ),
)
def ask_stream(req: AskRequest) -> StreamingResponse:
    # 生成器是惰性的：不先在这里调 load_settings()，配置错误会变成
    # "已经发出 200 响应头、然后流中断"，客户端很难处理。
    load_settings()

    relevant, top_score = is_relevant(req.question)

    try:
        hits = search_hybrid(req.question, top_k=req.top_k, mode=req.mode)
    except IndexEmptyError as exc:          # 交给全局处理器变 503，此时还没发响应头
        raise HTTPException(status_code=503, detail="向量库为空，请先构建索引") from exc

    hit_meta = [
        {"n": i, "doc_id": h["doc_id"],
         "section": h["section"].lstrip("#").strip(), "score": round(h["score"], 6)}
        for i, h in enumerate(hits, start=1)
    ]

    def event_stream():
        started = time.perf_counter()
        status = "ok"
        payload: dict = {"hits": len(hits)}
        if top_score:
            payload["gate"] = {"top_score": round(top_score, 4)}
        try:
            # 1) 先把命中片段发给前端，用户能立刻看到"在查什么"
            yield _sse({"type": "meta", "hits": hit_meta})

            # 2) 门槛拒绝 / 无命中：不发模型，直接给固定话术（与 /ask 一致）
            if not relevant or not hits:
                status = "refused"
                yield _sse({"type": "delta", "text": NO_CONTEXT_ANSWER})
                yield _sse({"type": "done", "citations": []})
                return

            # 3) 流式生成。一旦开始 yield 就不能再改状态码，
            #    模型错误只能以 error 事件的形式告诉前端。
            parts: list[str] = []
            try:
                for piece in chat_stream(build_messages(req.question, hits)):
                    parts.append(piece)
                    yield _sse({"type": "delta", "text": piece})
            except LLMError:
                status = "error"
                yield _sse({"type": "error", "message": "模型服务暂时不可用，请稍后重试"})
                return

            content = "".join(parts)
            citations = _extract_citations(content, hits)
            payload["citations"] = len(citations)
            yield _sse({"type": "done", "citations": [c.model_dump() for c in citations]})
        finally:
            # 流式响应在路由返回之后才结束，请求级连接早已关闭，
            # 所以这里必须用独立连接写 trace。
            record_trace_detached(kind="ask_stream", status=status,
                                  latency_ms=int((time.perf_counter() - started) * 1000),
                                  query=req.question, payload=payload)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        # 这两个头是流式的常规配套：禁缓存、禁 nginx 缓冲（否则会攒够一块才发）
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ==================== Agent 接口 ====================
# 和 /ask 的区别：/ask 是固定流水线（检索→作答）；
# 这里是模型自己决定调什么工具，而且写操作要经过 /agent/confirm 二次确认。

@app.post(
    "/agent/chat",
    response_model=AgentChatResponse,
    summary="客服 Agent 对话",
    description=(
        "让模型决定调用哪些工具（查订单、查工单、检索知识库）。"
        "如果它提议创建工单或改状态，不会立刻执行，而是返回 pending 等待 /agent/confirm。"
        "**注意：这个接口会多轮调用模型，耗时可能到数秒，客户端超时建议 60s 以上。**"
    ),
)
def agent_chat(
    req: AgentChatRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    # 路由只做三件事：接参数、调编排、返回。检索/工具/循环都在 agent 层。
    started = time.perf_counter()
    status = "ok"
    step_count: int | None = None
    payload: dict = {}
    try:
        result = run_agent(conn, session_id=req.session_id, message=req.message)
        steps = result.get("steps") or []
        step_count = len(steps)
        # 工具名列表进 payload：排查时一眼看出模型走了哪条路径
        payload["tools"] = [s["tool"] for s in steps]
        if result.get("pending"):
            status = "pending"
            payload["pending_tool"] = result["pending"]["tool"]
        return result
    except Exception as exc:
        status = "error"
        payload["error"] = type(exc).__name__
        raise
    finally:
        record_trace(conn, kind="agent_chat", status=status,
                     latency_ms=int((time.perf_counter() - started) * 1000),
                     session_id=req.session_id, query=req.message,
                     steps=step_count, payload=payload or None)


@app.post(
    "/agent/confirm",
    response_model=AgentConfirmResponse,
    summary="确认或拒绝待执行操作",
    description=(
        "对 /agent/chat 返回的 pending_id 做 approve / reject。"
        "重复 approve 同一 pending_id 是幂等的：返回第一次的结果，不会重复建单。"
        "确认单超过 30 分钟返回 409；session_id 不匹配返回 404。"
    ),
)
def agent_confirm(
    req: AgentConfirmRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    # 领域异常（NotFoundError/ConflictError）交给 errors.py 统一翻译成 404/409，
    # 这里不写 try/except——和 M6 的约定一致。
    started = time.perf_counter()
    status = "ok"
    payload: dict = {"decision": req.decision}
    try:
        result = confirm_pending(
            conn,
            pending_id=req.pending_id,
            session_id=req.session_id,
            decision=req.decision,
        )
        status = result.get("status", "ok")
        return result
    except Exception as exc:
        status = "error"
        payload["error"] = type(exc).__name__
        raise
    finally:
        record_trace(conn, kind="agent_confirm", status=status,
                     latency_ms=int((time.perf_counter() - started) * 1000),
                     session_id=req.session_id, payload=payload)


# ==================== 流式问答（M8） ====================
# 为什么单独一个接口而不是给 /ask 加 ?stream=true：
# 返回类型从 JSON 变成 SSE 流，客户端解析方式完全不同，混在一起两边都难写。

def _sse(payload: dict) -> str:
    """把 dict 编码成一条 SSE 事件。

    SSE 格式很简单：`data: <一行文本>\n\n`，空行表示事件结束。
    我们只在 data 里放一个 JSON，前端统一 json.loads 即可，
    不需要用 event: 字段区分类型（type 字段在 JSON 里）。
    """
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


@app.post(
    "/ask/stream",
    summary="流式知识库问答",
    description=(
        "与 `/ask` 逻辑相同，但用 SSE 逐字返回。事件类型："
        "`meta`（命中片段）→ 若干 `delta`（文本片段）→ `done`（引用）或 `error`。"
        "客户端按行读取，遇到空行结束一条事件。"
    ),
)
def ask_stream(req: AskRequest) -> StreamingResponse:
    # 生成器是惰性的：不先在这里调 load_settings()，配置错误会变成
    # "已经发出 200 响应头、然后流中断"，客户端很难处理。
    load_settings()

    try:
        hits = search_hybrid(req.question, top_k=req.top_k, mode=req.mode)
    except IndexEmptyError as exc:          # 交给全局处理器变 503，此时还没发响应头
        raise HTTPException(status_code=503, detail="向量库为空，请先构建索引") from exc

    hit_meta = [
        {"n": i, "doc_id": h["doc_id"],
         "section": h["section"].lstrip("#").strip(), "score": round(h["score"], 6)}
        for i, h in enumerate(hits, start=1)
    ]

    def event_stream():
        # 1) 先把命中片段发给前端，用户能立刻看到"在查什么"
        yield _sse({"type": "meta", "hits": hit_meta})

        # 2) 无命中：不发模型，直接给固定话术（行为与 /ask 一致）
        if not hits:
            yield _sse({"type": "delta", "text": NO_CONTEXT_ANSWER})
            yield _sse({"type": "done", "citations": []})
            return

        # 3) 流式生成。注意：一旦开始 yield，就不能再改 HTTP 状态码，
        #    所以模型错误只能以 error 事件的形式告诉前端。
        parts: list[str] = []
        try:
            for piece in chat_stream(build_messages(req.question, hits)):
                parts.append(piece)
                yield _sse({"type": "delta", "text": piece})
        except LLMError:
            yield _sse({"type": "error", "message": "模型服务暂时不可用，请稍后重试"})
            return

        content = "".join(parts)
        yield _sse({
            "type": "done",
            "citations": [c.model_dump() for c in _extract_citations(content, hits)],
        })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        # 这两个头是流式的常规配套：禁缓存、禁 nginx 缓冲（否则会攒够一块才发）
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ==================== 观测接口（M9） ====================

@app.get("/traces", summary="最近的调用记录", description="按时间倒序返回 trace 摘要；可按 kind 过滤。")
def list_traces_api(
    limit: int = Query(default=50, ge=1, le=200, description="返回条数"),
    kind: str | None = Query(default=None, description="ask / ask_stream / agent_chat / agent_confirm"),
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    return repo.list_traces(conn, limit=limit, kind=kind)


@app.get("/traces/{trace_id}", summary="单条 trace 详情")
def get_trace_api(
    trace_id: int = Path(description="trace id", examples=[1]),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    trace = repo.get_trace(conn, trace_id)
    if trace is None:
        raise repo.NotFoundError(f"trace {trace_id} 不存在")
    return trace


@app.get(
    "/metrics/summary",
    summary="调用汇总指标",
    description=(
        "总数、按类型/状态分布、延迟 p50/p95。"
        "注意：样本少于 20 条时 p95 意义有限，响应里会给出 count 供判断。"
    ),
)
def metrics_summary(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return repo.trace_stats(conn)


@app.post(
    "/agent/reset",
    summary="清空会话历史",
    description="删除该 session_id 下的 Agent 对话记录，返回删除条数。待确认单不受影响。",
)
def agent_reset(
    req: AgentResetRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    deleted = repo.clear_agent_messages(conn, req.session_id)
    return {"session_id": req.session_id, "deleted": deleted}
