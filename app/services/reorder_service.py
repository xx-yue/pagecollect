"""四阶段流水线编排。

这是整个服务的核心：把「一叠顺序错乱的扫描页」变成「正确顺序的 PDF」。
每个阶段都通过 task_store 更新进度，供客户端轮询。

    解析 → 逐页摘要（并发） → 全局排序（单次） → 重建

后台任务的异常一律落进任务状态而非向上抛——后台任务没有调用方接异常。
"""
import asyncio
from datetime import datetime
from typing import List, Optional

from app.config import settings
from app.core.log_config import get_logger
from app.core.task_store import task_store
from app.schemas.task import OrderResult, PageInfo, TaskStatus
from app.services import pdf_service
from app.services.llm_service import LLMClient, LLMError
from app.services.pdf_service import PagePayload

logger = get_logger(__name__)


async def run_pipeline(task_id: str, pdf_bytes: bytes, llm: LLMClient) -> None:
    """执行完整流水线。由接口层以 asyncio.create_task 启动。"""
    try:
        # ---------- 阶段 1：解析 ----------
        await _set(task_id, status=TaskStatus.PARSING, message="正在拆页解析")

        payloads = await asyncio.to_thread(
            pdf_service.parse_pdf,
            pdf_bytes,
            settings.render_dpi,
            settings.jpeg_quality,
            settings.text_layer_min_chars,
        )
        page_count = len(payloads)
        text_pages = sum(1 for p in payloads if p.text)
        await _set(
            task_id,
            page_count=page_count,
            total=page_count,
            message="共 {} 页（{} 页含文本层，走纯文本链路）".format(page_count, text_pages),
        )
        logger.info("任务 %s：解析完成，共 %d 页，%d 页含文本层", task_id, page_count, text_pages)

        # ---------- 阶段 2：逐页摘要（并发） ----------
        pages = await _summarize(task_id, payloads, llm)

        # ---------- 阶段 3：全局排序（单次） ----------
        order = await _order_with_retry(task_id, pages, llm)

        # ---------- 阶段 4：重建 ----------
        await _set(task_id, status=TaskStatus.REBUILDING, message="正在重建 PDF")
        result_bytes = await asyncio.to_thread(
            pdf_service.rebuild_pdf, pdf_bytes, order.ordered_indices
        )
        result_path = settings.result_dir / "{}.pdf".format(task_id)
        result_path.write_bytes(result_bytes)

        await _set(
            task_id,
            status=TaskStatus.DONE,
            progress=page_count,
            pages=pages,
            order=order,
            result_path=str(result_path),
            message="完成",
            finished_at=datetime.now(),
        )
        logger.info("任务 %s 完成，排序结果：%s", task_id, order.ordered_indices)

    except Exception as exc:  # noqa: BLE001 - 后台任务必须兜住所有异常
        logger.exception("任务 %s 失败", task_id)
        await _set(
            task_id,
            status=TaskStatus.FAILED,
            error=str(exc),
            message="处理失败",
            finished_at=datetime.now(),
        )


async def _summarize(
    task_id: str, payloads: List[PagePayload], llm: LLMClient
) -> List[PageInfo]:
    """并发逐页摘要。

    并发上限由 LLMClient 内部的 Semaphore 控制（settings.llm_concurrency），
    这里只管把所有页一起丢出去，让 asyncio 去调度。
    """
    total = len(payloads)
    results: List[Optional[PageInfo]] = [None] * total
    lock = asyncio.Lock()
    counter = {"done": 0}

    await _set(task_id, status=TaskStatus.SUMMARIZING, progress=0, message="正在逐页分析")

    async def worker(payload: PagePayload) -> None:
        info = await llm.describe_page(payload, total)
        results[payload.index - 1] = info
        async with lock:
            counter["done"] += 1
            await _set(
                task_id,
                progress=counter["done"],
                message="已分析 {}/{} 页".format(counter["done"], total),
            )

    # gather 默认遇到异常即抛出，其余 worker 会被取消——这是期望行为：
    # 缺页的排序没有意义，直接失败比产出错误结果更好。
    await asyncio.gather(*(worker(p) for p in payloads))

    missing = [i + 1 for i, r in enumerate(results) if r is None]
    if missing:
        raise LLMError("以下页面的摘要缺失：{}".format(missing))

    return [r for r in results if r is not None]


async def _order_with_retry(
    task_id: str, pages: List[PageInfo], llm: LLMClient
) -> OrderResult:
    """全局排序 + 全排列校验。

    模型偶尔会漏页或重复，必须校验；非法就整轮重试，
    连续 N 次非法才判定失败——绝不把错乱的顺序交给下游重建。
    """
    await _set(task_id, status=TaskStatus.ORDERING, message="正在推理正确页序")

    last_error: Optional[Exception] = None
    for attempt in range(1, settings.order_max_attempts + 1):
        try:
            order = await llm.order_pages(pages)
            order.validate_permutation(len(pages))
            return order
        except (LLMError, ValueError) as exc:
            last_error = exc
            logger.warning("任务 %s 第 %d 次排序结果非法：%s", task_id, attempt, exc)

    raise LLMError(
        "连续 {} 次排序结果非法，最后一次原因：{}".format(
            settings.order_max_attempts, last_error
        )
    )


async def _set(task_id: str, **fields) -> None:
    """更新任务状态的小助手，统一入口便于将来加钩子（如推送进度）。"""
    await task_store.update(task_id, **fields)
