from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,field_validator
#接口入参校验模型
class AskRequest(BaseModel) :
    # 这个示例会出现在 /docs 的 "Example Value" 里，方便调用方直接点"Try it out"
    model_config=ConfigDict(
        json_schema_extra={"examples":[{"question":"红字发票怎么开","top_k":4,"mode":"hybrid"}]}
    )
    #用户提问，长度1-500字
    question:str=Field(min_length=1,max_length=500,description="用户问题")
    #检索返回片段数量，默认为4，限制1-20
    top_k :int =Field(default=4,ge=1,le=20,description="检索片段数")
    #检索模式，只能{vector,bm25,hybrid}三选一,默认hybrid
    mode :Literal["vector","bm25","hybrid"]=Field(default="hybrid",description="检索模式")
    #字段校验装饰器，指定对question做自定义校验，当传入question完成基础类型校验后会自动执行下面这个函数
    @field_validator("question")
    @classmethod#标识这是一个类方法，第一个参数必须为cls
    def _not_blank(cls,value:str)->str :
        value=value.strip()
        if not value :
            raise ValueError("问题不能为空")
        return value
#用于回答里的引用标注
class Citation(BaseModel) :
    n :int
    doc :str
    section :str
#原始检索结果，包括序号，文档id，片段内容，相似度分数
class HitOut(BaseModel):
    n :int
    doc_id :str
    section :str
    score :float
#API返回体：answer：llm生成的最终答案，citation：回答中引用的文档片段，hits：完整检索命中列表，方便前端展示检索详情
class AskResponse(BaseModel) :
    answer :str
    citations :list[Citation]
    hits:list[HitOut]


    
# 工单状态 / 优先级的取值集合。
# 用 Literal 而不是 str 的原因：FastAPI 会把它写进 /docs 的下拉，
# 客户端传 "opne" 这种拼错值时直接 422，而不是让 SQL 静默返回空列表。
TicketStatus = Literal["open", "in_progress", "resolved", "closed", "escalated"]
Priority = Literal["low", "normal", "high", "urgent"]


# 工单的输出模型。
# 字段就是对外契约：仓库 dict 里如果多出列，会被 response_model 过滤掉，
# 所以新增内部字段不会意外泄露给客户端。
class TicketOut(BaseModel):
    id: str
    customer_id: str
    order_id: str | None = None    # 工单可以不关联订单
    title: str
    description: str
    status: TicketStatus
    priority: Priority
    created_at: str                # 库里存的是 UTC ISO 字符串，原样透传
    updated_at: str


# 事件输出模型。id 是自增整数，和工单的 TKT-xxx 不是一套编号。
class EventOut(BaseModel):
    id: int
    ticket_id: str
    event_type: str
    actor: str                     # 谁做的：cli / api / system
    content: str
    created_at: str


# 详情模型用继承：TicketDetail 自动拥有 TicketOut 的全部字段，
# 只额外加一个 events。这样加字段时只改父类，子类不用动。
class TicketDetail(TicketOut):
    events: list[EventOut]


class TicketCreate(BaseModel):
    model_config=ConfigDict(
        json_schema_extra={"examples":[{
            "customer_id":"C001",
            "title":"商品破损申请退款",
            "description":"收到货时外包装破损，内件有划痕",
            "priority":"high",
        }]}
    )
    customer_id: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    order_id: str | None = Field(default=None, max_length=32)
    priority: Priority = "normal"

    # 注意这里没有 actor，也没有 status：
    # actor 由服务端写死 "api"（审计字段不能让客户端伪造）；
    # status 由仓库层固定为 "open"（新工单不可能一创建就是 resolved）。


class StatusUpdate(BaseModel):
    model_config=ConfigDict(
        json_schema_extra={"examples":[{"status":"resolved","note":"已联系客户并退款"}]}
    )
    status: TicketStatus
    # note 给默认空串：状态变更必须留事件，但不该强制客户端想一句说明。
    # 空的时候由路由生成默认文案。
    note: str = Field(default="", max_length=200)
