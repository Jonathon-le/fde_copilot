import sqlite3
import re
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Path, Query, Response

from app.api.errors import register_error_handlers
from app.api.schemas import (
    AskRequest, AskResponse, Citation, HitOut,
    TicketCreate, TicketDetail, TicketOut, StatusUpdate, TicketStatus,
)
from app.config import get_settings
from app.db.connection import get_connection, init_db
from app.llm.client import chat
from app.rag.hybrid import search_hybrid
from app.rag.index import get_collection
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
@app.post("/ask",response_model=AskResponse,summary="知识库问答",description="检索知识库并让模型基于片段作答并给出引用；检索结果为空时返回固定话术，不调用模型。")
def ask(req:AskRequest)->AskResponse :
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
          return AskResponse(answer=NO_CONTEXT_ANSWER,citations=[],hits=[])
     #3)只有有上下文时才调用模型。LLM 失败由全局处理器变成 502
     content=chat(build_messages(req.question,hits))["content"]
     return AskResponse(
          answer=content,
          citations=_extract_citations(content,hits),
          hits=hit_out
     )

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
