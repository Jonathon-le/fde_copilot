
"""大模型客户端。

M3 先定义错误类型，再实现 chat()。HTTP 请求部分按任务卡自己补全。
"""
import json
import httpx

from app.config import get_settings
from app.config import Settings



class LLMError(Exception):
    """LLM 客户端所有错误的基类。

    上层（CLI、FastAPI）只需要捕获 LLMError，
    就能把配置问题、网络问题、响应格式问题统一处理。
    """


class LLMRequestError(LLMError):
    """请求阶段失败：连接失败、超时，或服务端返回非 2xx。

    status_code 为 None 表示没有拿到 HTTP 响应（连接失败、DNS 错误、超时）。
    有状态码时，上层可以据此决定是否重试：429 和 5xx 通常可重试，
    400/401/403/404 重试没有意义。

    注意：异常里只保存状态码和文本摘要，不保存 httpx.Response 对象。
    Response 上挂着 request.headers（含 Authorization），
    一旦被日志或 repr 打出来，API Key 就泄露了。
    """
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    def __str__(self) -> str:
        text = super().__str__()
        if self.status_code is None:
            return text
        return f"[HTTP {self.status_code}] {text}"

    
class LLMConfigError(LLMError) :
    """配置缺失或不合法:没有key，url为空，超时不是正数等。"""
class LLMResponseError(LLMError) :
    """响应能拿到，但结果不符合预期"""
def validate_settings(settings:Settings)->None :
    if not settings.llm_API_KEY.strip() :
        raise LLMConfigError(
            "缺少LLM_API_KEY。请先设置环境变量\n"
            'powershell当前窗口:$env:LLM_API_KEY="sk-你的key"\n'
            "如果只想验证CLI流程，可以加--mock"
        )
    base_url=settings.llm_BASE_URL.strip()
    if not base_url :
        raise LLMConfigError("LLM_BASE_URL不能为空")
    if not base_url.startswith(("http://","https://")) :
        raise LLMConfigError(f"LLM_BASE_URL必须以http://或https://开头，当前值：{base_url!r}")
    if not settings.llm_MODEL.strip() :
        raise LLMConfigError("LLM_MODEL不能为空")
    if settings.llm_timeout<=0 :
        raise LLMConfigError(f"LLM_TIMEOUT必须大于零，当前值{settings.llm_timeout}")
    if settings.llm_max_tokens<=0 :
        raise LLMConfigError(f"LLM_MAX_TOKENS必须大于零,当前值{settings.llm_max_tokens}")

def _parse_tool_calls(message:dict)->list[dict]:
    """把message.tool_calls解析成[{"id","name","arguments"}].
    
    arguments在协议里是json字符串；解析失败抛LLMResponsseError,
    不猜测，不跳过--猜出来的参数可能只想别人的订单。
    
    """
    raw=message.get("tool_calls") or []
    if not isinstance(raw,list):
        raise LLMResponseError(f"tool_calls应为列表，实际是{type(raw).__name__}")
    parsed=[]
    for i,item in enumerate(raw) :
        if not isinstance(item,dict) :
            raise LLMResponseError(f"tool_calls[{i}]不是对象")

        call_id=item.get("id")
        if not isinstance(call_id,str) or not call_id :
            raise LLMResponseError(f"tool_calls[{i}]缺少id")

        fn=item.get("function")
        if not isinstance(fn,dict):
            raise LLMResponseError(f"tool_calls[{i}]缺少function对象")

        name=fn.get("name")
        if not isinstance(name,str):
            raise LLMResponseError(f"tool_calls[{i}]缺少函数名")

        arguments=fn.get("arguments")
        if isinstance(arguments,dict):
            args=arguments
        elif isinstance(arguments,str):
            if not arguments.strip():
                args={}
            else:
                try :
                    args=json.loads(arguments)
                except json.JSONDecodeError as exc :
                    raise LLMResponseError(
                        f"tool_calls[{i}]的arguments不是合法JSON:{arguments[:200]!r}"
                    ) from exc
        else :
            raise LLMResponseError(
                f"tool_calls[{i}]的arguments类型不支持:{type(arguments).__name__}"
            )
        if not isinstance(args,dict):
            raise LLMResponseError(f"tool_calls[{i}]的arguments应为json对象")
        parsed.append({"id":call_id,"name":name,"arguments":args})
    return parsed

def load_settings() :
    try :
        settings=get_settings()
    except ValueError as exc :
        raise LLMConfigError (f"配置读取失败：{exc}")from exc
    validate_settings(settings)
    return settings
def chat (message:list[dict[str,str]],
*,
tools:list[dict]|None=None,
temperature:float=0.2,
max_tokens:int|None=None,
        )->dict :
    settings=load_settings()
    url=settings.llm_BASE_URL.strip().rstrip("/")+"/chat/completions"
    headers={"Authorization":f"Bearer {(settings.llm_API_KEY).strip()}","Content-Type":"application/json"}
    max_tok=max_tokens if max_tokens is not None else settings.llm_max_tokens
    payload={
  "model": settings.llm_MODEL,
  "messages": message,
  "temperature": temperature,
  "max_tokens": max_tok,
  "stream": False
            }
    if tools is not None :
        payload["tools"]=tools
        payload["tool_choice"]="auto"
    try :
        with httpx.Client(timeout=httpx.Timeout(settings.llm_timeout,connect=10.0)) as client :
            response=client.post(url,headers=headers,json=payload)
    except httpx.TimeoutException as exc :
        raise LLMRequestError("请求超时") from exc
    except httpx.ConnectError as exc :
        raise LLMRequestError("无法连接大模型服务") from exc
    except httpx.RequestError as exc :
        raise LLMRequestError(f"请求失败:{exc}") from exc
    #只有上面的try没有抛异常response才存在
    #response存在后检查响应是否正确
    if not 200<=response.status_code<300 :
        raise LLMRequestError(f"模型服务返回错误：{response.text[:300]}",status_code=response.status_code)
    try:
        data=response.json()
    #响应体不是jsoon，response.json抛json.JSONDecodeError
    except ValueError as exc :#JSONDecodeError是ValueError的子类
        raise LLMResponseError(
            f"响应不是合法Json:{response.text[:300]!r}"
            ) from exc
    #响应体是json但顶层不是对象
    if not isinstance(data,dict) :
        raise LLMResponseError(
            f"响应顶层应为JSON对象，实际是{type(data).__name__}"
        )
    #200但响应里带error字段
    if "error" in data :
        raise LLMResponseError(f"服务端返回错误：{str(data['error'])[:300]}"
                               )
    choices=data.get("choices")
    #choices缺失或choices为空
    if not isinstance(choices,list) or not choices :
        raise LLMResponseError(
            f"响应缺少choices或choices为空:{str(data)[:300]}"
        )
    first=choices[0]
    message_=first.get("message") if isinstance(first,dict) else None
    #choices[0]不是对象，message缺失1或不是对象
    if not isinstance(message_,dict):
        raise LLMResponseError("choices[0]缺少message对象")
    content=message_.get("content") or ""
    tool_calls=_parse_tool_calls(message_)
    #content+tool_calls为空
    if not  content.strip() and not tool_calls :
        raise LLMResponseError(
            f"响应里既没有content也没有tool_calls:{str(data)[:300]}"
            )
    return{
        "content":content,
        "tool_calls":tool_calls,
        "model":data.get("model"),
        "usage":data.get("usage")
    }
    
    