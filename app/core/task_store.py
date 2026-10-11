"""任务状态存储。

本版本用进程内存 + asyncio.Lock，重启即丢、不支持多 worker。
对 20~100 页的任务（约 1~2 分钟完成）完全够用；
若将来要持久化或跑多副本，把本类换成 Redis / SQLite 实现即可，对外方法签名保持不变。

注意：生产部署务必用单 worker（uvicorn --workers 1），
内存任务表在多个 worker 之间不共享。
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from app.schemas.task import OrderResult, PageInfo, TaskStatus


@dataclass
class TaskRecord:
    """单个任务的完整状态。"""

    task_id: str
    filename: str

    page_count: int = 0
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0
    total: int = 0
    message: str = ""
    error: Optional[str] = None

    # 逐页摘要（SUMMARIZING 阶段产出）
    pages: List[PageInfo] = field(default_factory=list)
    # 模型给出的全局排序（ORDERING 阶段产出）
    order: Optional[OrderResult] = None
    # 用户在客户端确认/微调后的最终顺序，优先级高于模型结果
    confirmed_order: Optional[List[int]] = None

    created_at: datetime = field(default_factory=datetime.now)
    finished_at: Optional[datetime] = None

    # 用字符串存路径，避免 dataclass 层依赖 Path
    source_path: str = ""
    result_path: str = ""


class TaskStore:
    def __init__(self) -> None:
        self._tasks: Dict[str, TaskRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, record: TaskRecord) -> TaskRecord:
        async with self._lock:
            self._tasks[record.task_id] = record
        return record

    async def get(self, task_id: str) -> Optional[TaskRecord]:
        # 读不加锁：CPython 下 dict.get 是原子操作，且引用替换不会撕裂
        return self._tasks.get(task_id)

    async def update(self, task_id: str, **fields) -> Optional[TaskRecord]:
        """按字段更新。注意只更新传入的字段，未传入的保持原值。"""
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return None
            for key, value in fields.items():
                setattr(record, key, value)
            return record

    async def delete(self, task_id: str) -> Optional[TaskRecord]:
        async with self._lock:
            return self._tasks.pop(task_id, None)

    async def list_all(self) -> List[TaskRecord]:
        return list(self._tasks.values())


task_store = TaskStore()
