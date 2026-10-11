"""应用入口。

开发启动：uvicorn app.main:app --reload
生产启动：uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1

注意 --workers 必须为 1：任务状态存放在进程内存中，多 worker 之间不共享，
多开会导致「上传到 A 进程、轮询打到 B 进程」从而查不到任务。
需要横向扩展时，把 core/task_store.py 换成 Redis 实现即可。
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.config import settings
from app.core.log_config import get_logger, setup_logging
from app.services.llm_service import close_llm_client

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动 / 关闭钩子。"""
    setup_logging()
    settings.ensure_dirs()
    logger.info(
        "%s 启动 | 模型模式：%s",
        settings.app_name,
        "MOCK（不联网，用于本地验证）"
        if settings.mock_llm
        else "真实 API（{}）".format(settings.llm_model),
    )
    yield
    await close_llm_client()
    logger.info("%s 已停止", settings.app_name)


app = FastAPI(
    title="pagecollate",
    description="扫描件页序智能重排服务：上传 PDF → 大模型判序 → 返回排序结果与成品",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")


@app.get("/health", tags=["system"])
async def health() -> dict:
    """健康检查。mock_llm 字段可一眼看出当前跑的是 mock 还是真实模型。"""
    return {
        "status": "ok",
        "app": settings.app_name,
        "mock_llm": settings.mock_llm,
        "model": settings.llm_model,
    }
