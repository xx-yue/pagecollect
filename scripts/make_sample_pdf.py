"""生成一份「物理顺序被打乱」的示例扫描件 PDF，用于全链路自测。

生成逻辑：
- 每页正文里写有 `逻辑页序: N` 标记（N 才是正确的阅读顺序）
- 但 PDF 的物理页序是打乱的（见 PHYSICAL_ORDER）

配合 MOCK_LLM=true，内置 mock 会读取这个标记完成排序，
于是可以零成本验证「上传 → 排序 → 重建」全链路是否打通。

用法：
    python scripts/make_sample_pdf.py [输出路径]
"""
import sys
from pathlib import Path

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24 推荐写法
except ImportError:  # pragma: no cover
    import fitz


# 逻辑顺序（正确阅读顺序）下的页面内容
LOGICAL_PAGES = [
    "会议纪要（封面）\n\n2026 年第三季度项目评审\n编制部门：技术中心",
    "一、项目背景\n\n本项目旨在解决纸质档案数字化过程中\n扫描页序错乱的问题。",
    "二、本期进展\n\n已完成扫描件解析、大模型判序、\nPDF 重建三个核心模块的开发。",
    "三、风险与问题\n\n1. 大模型判序存在一定的不确定性；\n2. 敏感文件上云需评估合规风险。",
    "四、下一步计划\n\n下阶段将接入真实多模态模型，\n并完成客户端联调。\n\n（本页为末页）",
]

# 物理页序 = 打乱后的逻辑序号。
# [3, 1, 5, 2, 4] 表示：输出 PDF 的第 1 页，内容是逻辑第 3 页。
PHYSICAL_ORDER = [3, 1, 5, 2, 4]

# 正确的排序结果（1-based 物理页号序列），供测试断言使用
EXPECTED_ORDER = [PHYSICAL_ORDER.index(i) + 1 for i in sorted(PHYSICAL_ORDER)]


def build_sample_pdf(output_path: Path) -> Path:
    """生成示例 PDF，返回写入的路径。"""
    doc = fitz.open()

    for physical_index, logic_index in enumerate(PHYSICAL_ORDER, start=1):
        page = doc.new_page()
        text = (
            "逻辑页序: {logic}\n"
            "{body}\n\n"
            "---\n"
            "（扫描物理顺序：第 {physical} 页）"
        ).format(
            logic=logic_index,
            body=LOGICAL_PAGES[logic_index - 1],
            physical=physical_index,
        )
        rect = fitz.Rect(72, 72, page.rect.width - 72, page.rect.height - 72)
        # 用 PyMuPDF 内置简体中文字体，避免依赖外部字体文件
        page.insert_textbox(rect, text, fontsize=14, fontname="china-s")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    doc.close()
    return output_path


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("sample_scrambled.pdf")
    path = build_sample_pdf(target)
    print("已生成示例 PDF：{}".format(path.resolve()))
    print("物理页序    ：{}".format(PHYSICAL_ORDER))
    print("正确逻辑页序：{}".format(sorted(PHYSICAL_ORDER)))
    print("期望返回的 ordered_indices = {}".format(EXPECTED_ORDER))


if __name__ == "__main__":
    main()
