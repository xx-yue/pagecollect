"""接口层数据结构。

设计要点：大模型的返回值必须能落进 PageInfo / OrderResult 两个模型才允许继续，
任何多余字段、类型不符、越界都会在这里被 Pydantic 拦下（防模型幻觉的第一道闸）。
"""
from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """任务状态机。客户端轮询时按 status 文案展示即可。"""

    PENDING = "PENDING"            # 已接收，排队中
    PARSING = "PARSING"            # 拆页 + 渲染
    SUMMARIZING = "SUMMARIZING"    # 逐页摘要（并发调模型）
    ORDERING = "ORDERING"          # 全局排序（单次调用）
    REBUILDING = "REBUILDING"      # 重建 PDF
    DONE = "DONE"                  # 完成
    FAILED = "FAILED"              # 失败，error 字段有原因


# --------------------------------------------------------------------------
# 大模型返回结构
# --------------------------------------------------------------------------

class PageInfo(BaseModel):
    """单页摘要结果。

    page_index 是『物理页序』（扫描时产生），模型只是原样回传；
    服务端会以本地解析结果为准强制覆盖，不采信模型写的值。
    """

    page_index: int = Field(description="物理页序，从 1 开始，原样回传")
    summary: str = Field(default="", description="本页内容摘要，30 字以内")
    doc_type: str = Field(default="", description="页面所属文档类型，如 合同/发票/简历")
    position_hint: str = Field(default="", description="位置线索：首页 / 末页 / 正文中间")


class OrderResult(BaseModel):
    """全局排序结果。ordered_indices 是正确顺序下的物理页序序列。"""

    ordered_indices: List[int] = Field(default_factory=list, description="正确顺序下的物理页序")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="整体置信度 0~1")
    reason: str = Field(default="", description="排序依据说明")

    def validate_permutation(self, page_count: int) -> None:
        """校验是否为 1..page_count 的合法全排列。

        这是防模型幻觉的核心一环：漏页、重复页、越界数值都必须拦下来，
        否则重建 PDF 会丢页或抛异常。非法直接抛 ValueError，由上层触发重试。
        """
        expected = list(range(1, page_count + 1))
        actual = sorted(self.ordered_indices)
        if actual != expected:
            raise ValueError(
                "模型返回的排列非法：期望 1..{} 的全排列，实际 {}".format(
                    page_count, self.ordered_indices
                )
            )


# --------------------------------------------------------------------------
# 对外接口结构
# --------------------------------------------------------------------------

class TaskCreatedResponse(BaseModel):
    """上传响应。

    page_count 是必须项：客户端拿它和本地页数核对，不一致要立即中止，
    否则后续返回的整数数组会整体错位（索引交换协议的第二条铁律）。
    """

    task_id: str
    page_count: int
    status: TaskStatus
    message: str = ""


class PageDetail(BaseModel):
    """单页明细，供客户端展示与人工复核。"""

    page_index: int
    summary: str = ""
    doc_type: str = ""
    position_hint: str = ""
    needs_review: bool = False   # 低置信度标记，客户端可据此高亮该页


class TaskDetailResponse(BaseModel):
    """完成后的完整结果：客户端用 ordered_indices 做本地重排与预览。"""

    task_id: str
    status: TaskStatus
    page_count: int
    ordered_indices: Optional[List[int]] = None
    confirmed_order: Optional[List[int]] = None   # 用户人工确认过的顺序（优先）
    confidence: Optional[float] = None
    reason: str = ""
    needs_review: bool = False
    pages: List[PageDetail] = Field(default_factory=list)


class TaskStatusResponse(BaseModel):
    """轮询用的轻量状态。"""

    task_id: str
    status: TaskStatus
    progress: int = 0       # 已完成步骤数
    total: int = 0          # 总步骤数（= 页数）
    page_count: int = 0
    message: str = ""
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class FinalizeRequest(BaseModel):
    """客户端提交人工微调后的最终顺序，服务端据此生成成品 PDF。"""

    ordered_indices: List[int] = Field(description="客户端确认的最终页序")

    def validate_permutation(self, page_count: int) -> None:
        if sorted(self.ordered_indices) != list(range(1, page_count + 1)):
            raise ValueError(
                "提交的顺序非法：期望 1..{} 的全排列，实际 {}".format(
                    page_count, self.ordered_indices
                )
            )


class FinalizeResponse(BaseModel):
    task_id: str
    ordered_indices: List[int]
    download_url: str
