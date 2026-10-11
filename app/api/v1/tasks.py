"""任务接口。

五个端点构成「索引交换协议」的完整闭环：

    POST   /tasks                  上传 → 返回 task_id + page_count（必须核对）
    GET    /tasks/{id}             轮询进度
    GET    /tasks/{id}/detail      拉取排序结果 + 每页摘要（人工复核依据）
    POST   /tasks/{id}/finalize    提交客户端确认后的最终顺序
    GET    /tasks/{id}/result      下载成品 PDF
    DELETE /tasks/{id}             清理任务与落盘文件

两条不可动摇的约束：
1. 上传响应必须返回 page_count，客户端须与本地页数核对——不一致即中止。
2. 排序结果必须是 1..N 的合法全排列，服务端与客户端各校验一次（双端防护）。
"""
import asyncio
import uuid
from pathlib import Path
from typing import Set

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.config import settings
from app.core.log_config import get_logger
from app.core.task_store import TaskRecord, task_store
from app.schemas.task import (
    FinalizeRequest,
    FinalizeResponse,
    PageDetail,
    TaskCreatedResponse,
    TaskDetailResponse,
    TaskStatus,
    TaskStatusResponse,
)
from app.services import pdf_service
from app.services.llm_service import get_llm_client
from app.services.reorder_service import run_pipeline

logger = get_logger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])

# 后台任务强引用集合。
# asyncio 对 create_task 只持弱引用，不自己保存的话任务可能中途被 GC 回收。
_running_tasks: Set[asyncio.Task] = set()


async def _require_task(task_id: str) -> TaskRecord:
    record = await task_store.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在或已清理")
    return record


@router.post("", response_model=TaskCreatedResponse, status_code=202)
async def create_task(file: UploadFile = File(...)) -> TaskCreatedResponse:
    """上传 PDF，立即返回 task_id 与页数，不等待排序完成（异步任务）。"""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="只接受 PDF 文件")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="文件内容为空")

    try:
        page_count = await asyncio.to_thread(pdf_service.count_pages, pdf_bytes)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="无法解析该 PDF：{}".format(exc))

    if page_count == 0:
        raise HTTPException(status_code=400, detail="PDF 没有任何页面")

    task_id = uuid.uuid4().hex[:12]
    source_path = settings.upload_dir / "{}.pdf".format(task_id)
    source_path.write_bytes(pdf_bytes)

    record = TaskRecord(
        task_id=task_id,
        filename=file.filename,
        page_count=page_count,
        total=page_count,
        source_path=str(source_path),
    )
    await task_store.create(record)

    task = asyncio.create_task(run_pipeline(task_id, pdf_bytes, get_llm_client()))
    _running_tasks.add(task)
    task.add_done_callback(_running_tasks.discard)

    logger.info("任务 %s 已创建：%s，共 %d 页", task_id, file.filename, page_count)
    return TaskCreatedResponse(
        task_id=task_id,
        page_count=page_count,
        status=record.status,
        message="已接收，共 {} 页".format(page_count),
    )


@router.get("/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str) -> TaskStatusResponse:
    """轮询接口。客户端建议 3 秒一次。"""
    record = await _require_task(task_id)
    return TaskStatusResponse(
        task_id=record.task_id,
        status=record.status,
        progress=record.progress,
        total=record.total,
        page_count=record.page_count,
        message=record.message,
        error=record.error,
        created_at=record.created_at,
        finished_at=record.finished_at,
    )


@router.get("/{task_id}/detail", response_model=TaskDetailResponse)
async def get_task_detail(task_id: str) -> TaskDetailResponse:
    """完成后的完整结果。

    ordered_indices 给客户端做本地重排；
    pages 里的摘要与置信度是人工复核的依据（低置信度页会被标记）。
    """
    record = await _require_task(task_id)
    if record.status != TaskStatus.DONE:
        raise HTTPException(
            status_code=409,
            detail="任务尚未完成，当前状态：{}".format(record.status.value),
        )

    order = record.order
    confidence = order.confidence if order else None
    needs_review = (
        confidence is not None and confidence < settings.low_confidence_threshold
    )

    return TaskDetailResponse(
        task_id=record.task_id,
        status=record.status,
        page_count=record.page_count,
        ordered_indices=order.ordered_indices if order else None,
        confirmed_order=record.confirmed_order,
        confidence=confidence,
        reason=order.reason if order else "",
        needs_review=needs_review,
        pages=[
            PageDetail(
                page_index=page.page_index,
                summary=page.summary,
                doc_type=page.doc_type,
                position_hint=page.position_hint,
                needs_review=needs_review,
            )
            for page in record.pages
        ],
    )


@router.post("/{task_id}/finalize", response_model=FinalizeResponse)
async def finalize_task(task_id: str, payload: FinalizeRequest) -> FinalizeResponse:
    """提交人工确认后的最终顺序，服务端据此生成成品 PDF。

    这是索引交换协议闭环的最后一步：客户端本地拖拽调整 → 回传最终顺序 →
    服务端出成品。这样客户端无需引入 qpdf 等 PDF 操作库也能拿到结果文件。
    """
    record = await _require_task(task_id)
    if record.status != TaskStatus.DONE:
        raise HTTPException(
            status_code=409,
            detail="任务尚未完成，当前状态：{}".format(record.status.value),
        )

    try:
        payload.validate_permutation(record.page_count)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    source_bytes = Path(record.source_path).read_bytes()
    result_bytes = await asyncio.to_thread(
        pdf_service.rebuild_pdf, source_bytes, payload.ordered_indices
    )
    result_path = settings.result_dir / "{}.pdf".format(task_id)
    result_path.write_bytes(result_bytes)

    await task_store.update(
        task_id,
        confirmed_order=payload.ordered_indices,
        result_path=str(result_path),
    )
    logger.info("任务 %s 已按用户确认顺序重新生成：%s", task_id, payload.ordered_indices)

    return FinalizeResponse(
        task_id=task_id,
        ordered_indices=payload.ordered_indices,
        download_url="/api/v1/tasks/{}/result".format(task_id),
    )


@router.get("/{task_id}/result")
async def download_result(task_id: str) -> FileResponse:
    """下载重排后的 PDF。若客户端提交过 finalize，则返回该顺序的成品。"""
    record = await _require_task(task_id)
    if not record.result_path or not Path(record.result_path).exists():
        raise HTTPException(
            status_code=404,
            detail="结果尚未生成，任务状态：{}".format(record.status.value),
        )

    stem = record.filename.rsplit(".", 1)[0] if record.filename else task_id
    return FileResponse(
        record.result_path,
        media_type="application/pdf",
        filename="{}_ordered.pdf".format(stem),
    )


@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id: str) -> None:
    """清理任务状态与落盘文件。客户端处理完后应主动调用，避免磁盘堆积。"""
    record = await task_store.delete(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    for path_str in (record.source_path, record.result_path):
        if not path_str:
            continue
        path = Path(path_str)
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            logger.warning("删除文件失败 %s：%s", path, exc)
