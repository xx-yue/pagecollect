"""PDF 解析与重建。

这一层只和 PDF 打交道，不碰大模型，便于单独测试。

两条关键设计：
1. parse_pdf 是「省钱开关」—— 有文本层的页走纯文本链路，无文本层才渲染成图，
   扫描仪若已自带 OCR 文本层，这条链路能把模型成本降到约 1/10。
2. rebuild_pdf 只重排页对象，不重新生成内容，原分辨率 / 签章 / 图层全部保留。
"""
import io
from typing import List, NamedTuple, Optional

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24 推荐写法
except ImportError:  # pragma: no cover - 兼容旧版本
    import fitz

from pypdf import PdfReader, PdfWriter


class PagePayload(NamedTuple):
    """单页载荷。

    二选一：
    - text 非空 → 走纯文本链路（便宜）
    - image 非空 → 走多模态链路（贵，但能看图）
    """

    index: int                    # 物理页序，从 1 开始
    text: str                     # 文本层内容，无文本层时为空串
    image: Optional[bytes]        # JPEG 字节，走文本链路时为 None


def parse_pdf(
    pdf_bytes: bytes,
    dpi: int = 150,
    jpeg_quality: int = 85,
    text_min_chars: int = 50,
) -> List[PagePayload]:
    """拆页并决定每页走哪条链路。

    Args:
        dpi: 渲染分辨率。150 对正文识别足够，再高只是白烧 token。
        text_min_chars: 文本层字符数阈值，超过则判定为「有文本层」。
    """
    payloads: List[PagePayload] = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for index, page in enumerate(doc, start=1):
            text = page.get_text("text").strip()
            image = None

            if len(text) < text_min_chars:
                # 无文本层（典型扫描件）→ 渲染成 JPEG 交给多模态模型
                pix = page.get_pixmap(dpi=dpi)
                image = pix.tobytes(output="jpeg", jpg_quality=jpeg_quality)
                text = ""

            payloads.append(PagePayload(index=index, text=text, image=image))
    return payloads


def count_pages(pdf_bytes: bytes) -> int:
    """只读页数，用于上传时立即返回给客户端核对（索引交换协议第二条铁律）。"""
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        return doc.page_count


def rebuild_pdf(pdf_bytes: bytes, ordered_indices: List[int]) -> bytes:
    """按给定顺序重建 PDF。

    ordered_indices 是 1-based 的物理页序排列，例如 [3, 1, 2, 5, 4]
    表示输出文件的第 1 页取原文件的第 3 页，依此类推。

    会先做全排列校验：非法直接抛 ValueError，绝不生成错乱文件。
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    page_count = len(reader.pages)

    if sorted(ordered_indices) != list(range(1, page_count + 1)):
        raise ValueError(
            "顺序非法：期望 1..{} 的全排列，实际 {}".format(page_count, ordered_indices)
        )

    writer = PdfWriter()
    for idx in ordered_indices:
        writer.add_page(reader.pages[idx - 1])

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
