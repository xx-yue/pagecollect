"""v1 版本路由汇总。将来新增 v2 时另建目录，不动这里。"""
from fastapi import APIRouter

from app.api.v1 import tasks

api_router = APIRouter()
api_router.include_router(tasks.router)
