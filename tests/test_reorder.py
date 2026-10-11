"""核心逻辑测试。全部基于 mock 模式，不联网、不花钱，可离线执行。

运行：
    pytest -v
"""
import asyncio
import io
import re
import sys
import time
from pathlib import Path

import pytest

# 让测试能直接 import app / scripts 包（不依赖 pytest 的 rootdir 推断）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

from app.config import settings
from app.schemas.task import OrderResult
from app.services import pdf_service
from app.services.llm_service import LLMClient, _mock_describe, _mock_order
from app.services.pdf_service import PagePayload
from scripts.make_sample_pdf import EXPECTED_ORDER, PHYSICAL_ORDER, build_sample_pdf


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    """生成一份物理顺序被打乱的示例 PDF。"""
    return build_sample_pdf(tmp_path / "sample.pdf")


@pytest.fixture
def temp_storage(tmp_path: Path, monkeypatch):
    """把落盘目录改到临时目录，避免污染项目 storage/。"""
    storage = tmp_path / "storage"
    monkeypatch.setattr(settings, "storage_dir", storage)
    settings.ensure_dirs()
    return storage


def _extract_logic_sequence(pdf_bytes: bytes):
    """从 PDF 每页文本里抽出「逻辑页序: N」的 N，按页序返回列表。"""
    sequence = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            text = page.get_text("text")
            match = re.search(r"逻辑页序[:：]\s*(\d+)", text)
            sequence.append(int(match.group(1)) if match else None)
    return sequence


# --------------------------------------------------------------------------
# 全排列校验：防模型幻觉的第一道闸
# --------------------------------------------------------------------------

def test_validate_permutation_accepts_valid():
    OrderResult(ordered_indices=[3, 1, 2], confidence=0.9).validate_permutation(3)
    OrderResult(ordered_indices=[1], confidence=1.0).validate_permutation(1)


@pytest.mark.parametrize(
    "bad_order,page_count",
    [
        ([1, 2, 2], 3),      # 重复页
        ([1, 2], 3),         # 漏页
        ([1, 2, 4], 3),      # 越界
        ([0, 1, 2], 3),      # 0 起始（本协议一律 1-based）
        ([], 3),             # 空
    ],
)
def test_validate_permutation_rejects_invalid(bad_order, page_count):
    with pytest.raises(ValueError):
        OrderResult(ordered_indices=bad_order, confidence=0.9).validate_permutation(page_count)


# --------------------------------------------------------------------------
# PDF 解析与重建
# --------------------------------------------------------------------------

def test_parse_pdf_splits_all_pages(sample_pdf: Path):
    payloads = pdf_service.parse_pdf(sample_pdf.read_bytes())
    assert len(payloads) == len(PHYSICAL_ORDER)
    assert [p.index for p in payloads] == list(range(1, len(PHYSICAL_ORDER) + 1))
    # 示例 PDF 由脚本生成、带文本层，应全部走纯文本链路（省钱链路）
    assert all(p.text for p in payloads)
    assert all(p.image is None for p in payloads)


def test_rebuild_pdf_restores_logical_order(sample_pdf: Path):
    pdf_bytes = sample_pdf.read_bytes()
    rebuilt = pdf_service.rebuild_pdf(pdf_bytes, EXPECTED_ORDER)

    assert rebuilt[:4] == b"%PDF"
    sequence = _extract_logic_sequence(rebuilt)
    assert sequence == sorted(sequence), "重建后每页的逻辑页序应当递增"
    assert sequence == list(range(1, len(PHYSICAL_ORDER) + 1))


def test_rebuild_pdf_rejects_invalid_order(sample_pdf: Path):
    pdf_bytes = sample_pdf.read_bytes()
    with pytest.raises(ValueError):
        pdf_service.rebuild_pdf(pdf_bytes, [1, 2, 2, 3, 4])


def test_count_pages(sample_pdf: Path):
    assert pdf_service.count_pages(sample_pdf.read_bytes()) == len(PHYSICAL_ORDER)


# --------------------------------------------------------------------------
# Mock 大模型：两阶段判序逻辑
# --------------------------------------------------------------------------

def test_mock_describe_reads_logic_marker():
    payload = PagePayload(index=2, text="逻辑页序: 4\n正文……", image=None)
    info = _mock_describe(payload)
    assert info.page_index == 2
    assert "逻辑页序=4" in info.summary


def test_mock_order_sorts_by_logic_marker():
    pages = [
        _mock_describe(PagePayload(index=i, text="逻辑页序: {}".format(v), image=None))
        for i, v in enumerate(PHYSICAL_ORDER, start=1)
    ]
    order = _mock_order(pages)
    order.validate_permutation(len(pages))
    assert order.ordered_indices == EXPECTED_ORDER


def test_two_stage_pipeline_with_mock_client(sample_pdf: Path):
    """核心链路：逐页摘要 → 全局排序，结果必须是合法全排列且还原正确。"""
    pdf_bytes = sample_pdf.read_bytes()
    payloads = pdf_service.parse_pdf(pdf_bytes)
    client = LLMClient()

    async def run():
        try:
            pages = []
            for payload in payloads:
                pages.append(await client.describe_page(payload, len(payloads)))
            return await client.order_pages(pages)
        finally:
            await client.aclose()

    order = asyncio.run(run())
    order.validate_permutation(len(payloads))
    assert order.ordered_indices == EXPECTED_ORDER


# --------------------------------------------------------------------------
# HTTP 全链路
# --------------------------------------------------------------------------

def test_http_end_to_end(sample_pdf: Path, temp_storage):
    """上传 → 轮询 → 详情 → 下载，四个端点串通。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"

        # 1) 上传：响应必须带 page_count（索引交换协议第二条铁律）
        with open(str(sample_pdf), "rb") as fh:
            resp = client.post(
                "/api/v1/tasks",
                files={"file": ("sample.pdf", fh, "application/pdf")},
            )
        assert resp.status_code == 202, resp.text
        created = resp.json()
        task_id = created["task_id"]
        assert created["page_count"] == len(PHYSICAL_ORDER)

        # 2) 轮询直到完成
        status_body = {}
        for _ in range(150):
            status_body = client.get("/api/v1/tasks/{}".format(task_id)).json()
            if status_body["status"] in ("DONE", "FAILED"):
                break
            time.sleep(0.2)
        assert status_body["status"] == "DONE", status_body

        # 3) 详情：排序结果 + 每页摘要
        detail = client.get("/api/v1/tasks/{}/detail".format(task_id)).json()
        assert detail["ordered_indices"] == EXPECTED_ORDER
        assert len(detail["pages"]) == len(PHYSICAL_ORDER)

        # 4) 下载结果
        result = client.get("/api/v1/tasks/{}/result".format(task_id))
        assert result.status_code == 200
        assert result.content[:4] == b"%PDF"

        # 5) 人工确认顺序后重新生成
        finalize = client.post(
            "/api/v1/tasks/{}/finalize".format(task_id),
            json={"ordered_indices": list(reversed(EXPECTED_ORDER))},
        )
        assert finalize.status_code == 200, finalize.text
        sequence = _extract_logic_sequence(
            client.get("/api/v1/tasks/{}/result".format(task_id)).content
        )
        assert sequence == list(range(len(PHYSICAL_ORDER), 0, -1)), "应按用户提交的顺序重排"

        # 6) 清理
        assert client.delete("/api/v1/tasks/{}".format(task_id)).status_code == 204
        assert client.get("/api/v1/tasks/{}".format(task_id)).status_code == 404


def test_finalize_rejects_invalid_order(sample_pdf: Path, temp_storage):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        with open(str(sample_pdf), "rb") as fh:
            task_id = client.post(
                "/api/v1/tasks",
                files={"file": ("sample.pdf", fh, "application/pdf")},
            ).json()["task_id"]

        for _ in range(150):
            if client.get("/api/v1/tasks/{}".format(task_id)).json()["status"] == "DONE":
                break
            time.sleep(0.2)

        bad = client.post(
            "/api/v1/tasks/{}/finalize".format(task_id),
            json={"ordered_indices": [1, 1, 2, 3, 4]},
        )
        assert bad.status_code == 400


def test_upload_rejects_non_pdf(temp_storage):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/tasks",
            files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
        )
        assert resp.status_code == 400
