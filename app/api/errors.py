import logging

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.db.repositories import ConflictError, NotFoundError, ValidationError
from app.llm.client import LLMError
from app.rag.index import IndexEmptyError

logger = logging.getLogger(__name__)

# 领域异常的映射表：(异常类型, 状态码, error.type, 是否把异常原文给客户端)
# 放成一张表而不是写五个函数：加新异常只加一行，不会漏改。
# 顺序有意义：ConflictError 继承 ValidationError，两个都注册时 Starlette
# 会沿 MRO 找最近的，所以子类必须能被单独匹配到（这里显式注册即可）。
ERROR_SPECS = [
    (NotFoundError,   404, "not_found",        True),
    (ConflictError,   409, "conflict",         True),
    (ValidationError, 400, "validation_error", True),
    (IndexEmptyError, 503, "index_empty",      False),
    (LLMError,        502, "upstream_error",   False),
]

# expose=False 时用的固定文案。
# 为什么不能给客户端原文：LLMError 里可能有上游 URL、状态码、响应体片段，
# 这些东西对调用方没用，对攻击者有用。
SAFE_MESSAGES = {
    "index_empty": "知识库索引为空，请先构建索引",
    "upstream_error": "模型服务暂时不可用，请稍后重试",
}


def _domain_handler(status: int, error_type: str, expose: bool):
    """生成一个异常处理器。闭包把三个参数固定住。"""

    async def handler(request, exc):
        message = str(exc) if expose else SAFE_MESSAGES[error_type]
        # 只有 5xx 才记 error 日志。4xx 是调用方的错，天天记会把真正的故障埋掉。
        if status >= 500:
            logger.exception("domain error: %s", error_type)
        return JSONResponse(
            status_code=status,
            content={"error": {"type": error_type, "message": message}},
        )

    return handler


async def http_exception_handler(request, exc: StarletteHTTPException):
    # 路由里用 HTTPException 抛的错（如 404 兜底）也包成同一结构，
    # 否则客户端要处理两种错误格式。
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"type": "http_error", "message": str(exc.detail)}},
        headers=getattr(exc, "headers", None),
    )


async def validation_exception_handler(request, exc: RequestValidationError):
    # 422 的结构化字段很有用，不能丢；只在外面包一层统一信封。
    fields = [
        {"loc": ".".join(str(p) for p in err["loc"]), "msg": err["msg"]}
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "type": "validation_error",
                "message": "请求参数不合法",
                "fields": fields,
            }
        },
    )


async def unhandled_exception_handler(request, exc: Exception):
    # 兜底：任何没预料到的异常都变成统一结构的 500，
    # 堆栈只进日志，不进响应体。
    logger.exception("unhandled error")
    return JSONResponse(
        status_code=500,
        content={"error": {"type": "internal_error", "message": "服务内部错误"}},
    )


def register_error_handlers(app: FastAPI) -> None:
    for exc_type, status, error_type, expose in ERROR_SPECS:
        app.add_exception_handler(exc_type, _domain_handler(status, error_type, expose))
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
