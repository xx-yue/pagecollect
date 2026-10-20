"""pagecollate FastAPI 应用入口（最小骨架）。

启动方式：conda activate web_learn 后执行
    uvicorn app.main:app --reload
探活检查：GET /health
"""

from fastapi import FastAPI

app = FastAPI(title="pagecollate", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    """探活接口：确认服务已启动并正常响应。"""
    return {"status": "ok"}
