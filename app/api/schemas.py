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


# ==================== Agent 相关模型 ====================
# 注意和 /ask 的区别：/ask 是"一问一答"，这里是"一问多步 + 可能需要确认"。

class AgentStep(BaseModel):
    """一步工具调用的记录。M9 的 trace 页面会直接消费这个结构。"""
    step: int
    tool: str
    args: dict            # 工具参数原样返回，方便前端展示"模型想做什么"
    ok: bool
    elapsed_ms: int
    action: str           # executed / pending / repeat_abort


class PendingOut(BaseModel):
    """等待用户确认的写操作。

    故意包含 arguments：用户要能看懂"将创建什么"，而不是只看到一个 tool 名。
    """
    pending_id: str
    tool: str
    arguments: dict
    expires_at: str       # ISO 时间；前端可以倒计时，服务端以 created_at 为准


class AgentChatRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{
            "session_id": "3f2b8c1e-5a4d-4e6f-9a7b-2c1d0e9f8a7b",
            "message": "订单 O001 收到时破了，帮我登记一下退款",
        }]}
    )
    # session_id 由客户端生成（建议 UUID）：服务端不存会话内容，
    # 但它用来绑定待确认操作——换一个 session 就确认不了别人的单。
    session_id: str = Field(min_length=4, max_length=64, description="客户端生成的会话标识")
    message: str = Field(min_length=1, max_length=500, description="用户这句话")

    @field_validator("message")
    @classmethod
    def _message_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("消息不能为空")
        return value


class AgentChatResponse(BaseModel):
    answer: str
    steps: list[AgentStep] = Field(default_factory=list)
    # pending 非空 = 有写操作在等确认，此时 answer 可能是上一轮的（通常为空）
    pending: PendingOut | None = None


class AgentConfirmRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{
            "pending_id": "PA-1A2B3C4D",
            "session_id": "3f2b8c1e-5a4d-4e6f-9a7b-2c1d0e9f8a7b",
            "decision": "approve",
        }]}
    )
    pending_id: str = Field(min_length=1, max_length=32)
    session_id: str = Field(min_length=4, max_length=64)
    # Literal 而不是 str：传 "yes"/"ok" 这类词直接 422，
    # 避免在业务代码里写 if decision in ("approve","yes","ok") 这种模糊判断。
    decision: Literal["approve", "reject"]


class AgentConfirmResponse(BaseModel):
    pending_id: str
    status: Literal["executed", "failed", "rejected"]
    result: dict | None = None
