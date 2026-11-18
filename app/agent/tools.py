from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any,Callable,Literal

from pydantic import BaseModel,Field
from pydantic import ValidationError as PydanticValidationError

from app.db import repositories as repo
from app.rag.hybrid import search_hybrid

logger=logging.getLogger(__name__)
TicketStatus=Literal["open","in_progress","resolved","closed","escalated"]

#参数模型:同时扮演“给模型看的schema”和“执行前的校验”
#Pydantic Field 给模型字段加校验规则，默认值，描述文本
class SearchKnowledgeArgs(BaseModel):
    query:str=Field(min_length=1,max_length=200,description="要检索的问题，用用户的原始说法")
    top_k:int=Field(default=4,ge=1,le=8,description="返回片段数，默认4")

class GetCustomerArgs(BaseModel) :
    customer_id :str=Field(min_length=1,max_length=32,description="顾客号，形如C001")

class GetOrderArgs(BaseModel) :
    order_id :str=Field(min_length=1,max_length=32,description="订单号，形如O001")

class GetTicketArgs(BaseModel) :
    ticket_id :str=Field(min_length=1,max_length=32,description="工单号，形如TKT-1001")

class ListTicketsArgs(BaseModel) :
    customer_id :str|None=Field(default=None,description="按客户过滤，形如C001")
    status:TicketStatus|None=Field(default=None,description="按状态过滤")


#统一的返回形状
def _ok(**kwargs)->dict :
    return {"ok":True,**kwargs}

def _fail(reason:str,message:str,**extra)->dict :
    return{"ok":False,"reason":reason,"error":message,**extra}


#--------各工具的执行体------------
#统一签名（conn，已校验的参数对象）->dict,方便注册表调用

def _tool_search_knowledge(conn,args:SearchKnowledgeArgs)->dict :
    hits=search_hybrid(args.query,top_k=args.top_k)
    return _ok(hits=[{"doc_id":h["doc_id"],"section":h["section"].lstrip("#").strip(),"text":h["text"][:400]}
                     for h in hits
                     ]
               )

def _tool_get_order(conn,args:GetOrderArgs)->dict :
    order=repo.get_order(conn,args.order_id)
    if order is None :
        return _fail("not_found",f"订单{args.order_id}不存在")
    return _ok(order={
        k:order[k] for k in ("id","customer_id","status","amount","tracking_no","carrier","created_at")

    })

def _tool_get_customer(conn,args:GetCustomerArgs)->dict :
    customer=repo.get_customer(conn,args.customer_id)
    if customer is None :
        return _fail("not_found",f"客户{args.customer_id}不存在")
    #手机号/邮箱不回灌给模型：用不上，而且是PII
    return _ok(customer={"id":customer["id"],"name":customer["name"],"level":customer["level"]})

def _tool_get_ticket(conn,args:GetTicketArgs)->dict :
    ticket=repo.get_ticket(conn,args.ticket_id)
    if ticket is None :
        return _fail("not_found",f"工单{args.ticket_id}不存在")
    events=repo.list_ticket_events(conn,args.ticket_id)[-3:]#只要最近三条
    return _ok(
        ticket={
            "id":ticket["id"],"customer_id":ticket["customer_id"],"order_id":ticket["order_id"],
            "title":ticket["title"],"status":ticket["status"],"priority":ticket["priority"],
            "created_at":ticket["created_at"],
        },
        recent_event=[
            {
                "type":e["event_type"],"content":e["content"][:120],"at":e["created_at"]
            }
            for e in events
        ],
                )

def _tool_list_tickets(conn,args:ListTicketsArgs)->dict :
    tickets=repo.list_tickets(conn,status=args.status,customer_id=args.customer_id)
    return _ok(count=len(tickets),tickets=[
        {"id":t["id"],"status":t["status"],"title":t["title"],"created_at":t["created_at"]}
        for t in tickets[:10]
    ])

#--------------------注册表--------------------------
@dataclass(frozen=True)
class ToolSpec :
    name:str
    description:str
    args_model:type[BaseModel]
    func:Callable[[sqlite3.Connection,Any],dict]
    write:bool=False

READ_TOOLS :dict[str,ToolSpec]={
    spec.name:spec for spec in[
        ToolSpec(
            name="search_knowledge",
            description=(
                "检索企业知识库，回答政策，流程，规则类问题(退款政策，发票，会员积分等)。"
                "当问题涉及'能不能/怎么办理/多久到账'时使用。不用于查询具体订单或工单。"
            ),
            args_model=SearchKnowledgeArgs,func=_tool_search_knowledge,
        ),
        ToolSpec(
            name="get_order",
            description="按订单号查询订单的状态、金额、物流。用户提到'我的订单'时使用；需要订单号。",
            args_model=GetOrderArgs,func=_tool_get_order,
        ),
        ToolSpec(
            name="get_customer",
            description="按客户号查询客户等级等基本信息。需要确认客户身份或等级时使用。",
            args_model=GetCustomerArgs,func=_tool_get_customer
        ),
        ToolSpec(
            name="get_ticket",
            description="按工单号查询工单详情和最近事件。用户问'我的工单怎么样了'时使用。",
            args_model=GetTicketArgs,func=_tool_get_ticket,

        ),
        ToolSpec(
            name="list_ticket",
            description="列出工单，可按客户号或状态过滤。用户问'我有哪些工单'时使用。",
            args_model=ListTicketsArgs,func=_tool_list_tickets,
        ),
        
    ]
}

WRITE_TOOLS:dict[str,ToolSpec]={}

def schema_for_model(tools:dict[str,ToolSpec]|None=None)->list[dict]:
    tools=READ_TOOLS if tools is None else tools
    return[
        {
            "type":"function",
            "function":{
                "name":spec.name,
                "description":spec.description,
                "parameters":spec.args_model.model_json_schema(),
                                        },
        }
        for spec in tools.values()
    ]
#---------------唯一的读工具入口----------------

def execute_read(name:str,arguments:dict,conn:sqlite3.Connection)->dict:
    spec=READ_TOOLS.get(name)
    if spec is None :
        return _fail("unknown_tool",f"工具{name!r}不存在或不可调用")
    try :
        parsed=spec.args_model.model_validate(arguments)
    except PydanticValidationError as exc :
            #不抛异常：模型看到结构化错误可以改参数重试
            return _fail("invalid_args", "参数不合法", fields=[
            {"loc": ".".join(str(p) for p in err["loc"]), "msg": err["msg"]}
            for err in exc.errors()
            ])
    try :
        return spec.func(conn,parsed)
    except(repo.NotFoundError,repo.ValidationError) as exc :
        return _fail("domain_error",str(exc))
    except Exception :
        #意想不到的异常也不能让它把整个会话打崩:记日志，给模型一个可继续的失败结果
        logger.exception("tool %s failed",name)
        return _fail("internal_error","工具执行失败，请换一种方式再试")