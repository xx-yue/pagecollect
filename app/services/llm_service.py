"""大模型调用层。

统一走 OpenAI 兼容协议，换厂商只需改 .env 里的 base_url / model，业务代码一行不动。

两种模式：
- mock_llm=True ：本地假实现，不联网不花钱，用于跑通全链路、写测试、做演示
- mock_llm=False：真实调用多模态 / 文本模型
"""
import asyncio
import base64
import json
import re
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings
from app.core.log_config import get_logger
from app.prompts import reorder as prompts
from app.schemas.task import OrderResult, PageInfo
from app.services.pdf_service import PagePayload

logger = get_logger(__name__)


class LLMError(RuntimeError):
    """大模型调用失败（网络、鉴权、返回格式异常等）。"""


class LLMClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url.rstrip("/"),
            headers={
                "Authorization": "Bearer {}".format(settings.llm_api_key),
                "Content-Type": "application/json",
            },
            timeout=settings.llm_timeout,
        )
        # 并发闸门：同时最多 N 个请求在飞，避免触发厂商限流
        self._sem = asyncio.Semaphore(settings.llm_concurrency)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # 底层：带指数退避的 JSON 调用
    # ------------------------------------------------------------------

    async def _chat_json(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """调用 chat/completions 并解析出 JSON 对象。

        失败按 1s / 2s / 4s 退避重试；限流与网络抖动都能自愈。
        """
        last_error: Optional[Exception] = None

        for attempt in range(1, settings.llm_max_retries + 1):
            try:
                async with self._sem:
                    resp = await self._client.post(
                        "/chat/completions",
                        json={
                            "model": settings.llm_model,
                            "messages": messages,
                            "temperature": settings.llm_temperature,
                            "response_format": {"type": "json_object"},
                        },
                    )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                return self._loads(content)
            except Exception as exc:  # noqa: BLE001 - 需兜住所有网络与解析异常
                last_error = exc
                wait = 2 ** (attempt - 1)
                logger.warning(
                    "大模型调用失败（第 %d/%d 次）：%s；%d 秒后重试",
                    attempt, settings.llm_max_retries, exc, wait,
                )
                if attempt < settings.llm_max_retries:
                    await asyncio.sleep(wait)

        raise LLMError("大模型调用连续失败：{}".format(last_error))

    @staticmethod
    def _loads(content: str) -> Dict[str, Any]:
        """解析模型返回。容忍它把 JSON 包在 markdown 代码块里的情况。"""
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError("模型返回的不是合法 JSON：{}".format(text[:200])) from exc

        if not isinstance(data, dict):
            raise LLMError("模型返回的 JSON 顶层不是对象：{}".format(text[:200]))
        return data

    # ------------------------------------------------------------------
    # 阶段一：单页摘要
    # ------------------------------------------------------------------

    async def describe_page(self, payload: PagePayload, total: int) -> PageInfo:
        if settings.mock_llm:
            return _mock_describe(payload)

        if payload.image:
            content_hint = "页面以图片形式提供，请直接看图分析。"
        else:
            content_hint = "页面文本内容如下：\n\n{}".format(payload.text[:6000])

        user_text = prompts.PAGE_SUMMARY_USER.format(
            index=payload.index, total=total, content_hint=content_hint
        )

        if payload.image:
            b64 = base64.b64encode(payload.image).decode("ascii")
            user_content: Any = [
                {"type": "text", "text": user_text},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,{}".format(b64)},
                },
            ]
        else:
            user_content = user_text

        data = await self._chat_json(
            [
                {"role": "system", "content": prompts.PAGE_SUMMARY_SYSTEM},
                {"role": "user", "content": user_content},
            ]
        )

        # 关键：page_index 一律以本地解析结果为准，不采信模型写回的值。
        # 模型偶尔会写错编号，一旦错位则后续全盘皆错，这里必须钉死。
        data["page_index"] = payload.index
        return PageInfo(**data)

    # ------------------------------------------------------------------
    # 阶段二：全局排序
    # ------------------------------------------------------------------

    async def order_pages(self, pages: List[PageInfo]) -> OrderResult:
        """把全部 N 条摘要一次性交给模型。

        绝不能改成「逐页问模型该排第几」——单页没有全局视野，结果不可用。
        """
        if settings.mock_llm:
            return _mock_order(pages)

        data = await self._chat_json(
            [
                {"role": "system", "content": prompts.ORDER_SYSTEM},
                {
                    "role": "user",
                    "content": prompts.ORDER_USER_TEMPLATE.format(
                        total=len(pages),
                        pages_block=prompts.build_pages_block(pages),
                    ),
                },
            ]
        )
        return OrderResult(**data)


# --------------------------------------------------------------------------
# Mock 实现：不联网跑通全链路
# --------------------------------------------------------------------------

_MOCK_LOGIC_RE = re.compile(r"逻辑页序[:：]\s*(\d+)")


def _mock_describe(payload: PagePayload) -> PageInfo:
    """Mock 摘要。

    如果页面文本里有 `逻辑页序: N` 标记（scripts/make_sample_pdf.py 生成的示例
    PDF 会写这个标记），就把 N 带进摘要，好让 _mock_order 真的能排对，
    从而验证「上传 → 排序 → 重建」全链路是通的。
    """
    logic = None
    if payload.text:
        match = _MOCK_LOGIC_RE.search(payload.text)
        if match:
            logic = int(match.group(1))

    if logic is not None:
        summary = "[MOCK] 逻辑页序={}".format(logic)
    else:
        summary = "[MOCK] 第 {} 页（无逻辑标记）".format(payload.index)

    return PageInfo(
        page_index=payload.index,
        summary=summary,
        doc_type="mock",
        position_hint="",
    )


def _mock_order(pages: List[PageInfo]) -> OrderResult:
    """Mock 排序：优先按摘要里的『逻辑页序=N』排序，否则保持原顺序。"""

    def sort_key(page: PageInfo) -> int:
        match = re.search(r"逻辑页序=(\d+)", page.summary)
        return int(match.group(1)) if match else page.index

    ordered = [page.page_index for page in sorted(pages, key=sort_key)]
    return OrderResult(
        ordered_indices=ordered,
        confidence=1.0,
        reason="[MOCK] 依据页面内的『逻辑页序』标记排序",
    )


# --------------------------------------------------------------------------
# 单例管理：httpx.AsyncClient 需要复用，在应用生命周期内只创建一次
# --------------------------------------------------------------------------

_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


async def close_llm_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
