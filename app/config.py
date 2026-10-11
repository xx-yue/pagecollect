"""全局配置。

所有可调参数集中在这里，通过项目根目录的 .env 覆盖。
字段含义见注释；改动前先看 README 的「配置说明」一节。
"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- 基本信息 ----------
    app_name: str = "pagecollate"
    debug: bool = True

    # 允许跨域的来源。Qt 客户端不受 CORS 限制，此项仅为浏览器调试方便。
    # 生产环境请改成具体域名，不要留 ["*"]
    cors_origins: list = ["*"]

    # ---------- 存储 ----------
    # 上传原件与结果文件的落盘根目录
    storage_dir: Path = BASE_DIR / "storage"

    # ---------- 大模型 ----------
    # True ：不调用真实 API，用内置 mock 逻辑跑通全链路（开发 / 演示 / 自测用）
    # False：走 OpenAI 兼容协议调用真实多模态模型
    mock_llm: bool = True

    # OpenAI 兼容端点。常见国内厂商：
    #   通义千问  https://dashscope.aliyuncs.com/compatible-mode/v1
    #   智谱 GLM  https://open.bigmodel.cn/api/paas/v4
    #   豆包      https://ark.cn-beijing.volces.com/api/v3
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_api_key: str = ""
    llm_model: str = "qwen-vl-max"

    # 并发调用页数。受厂商 RPM 限制约束，建议 3~8。
    # 调太高会频繁触发限流——本服务内置指数退避重试，但排队等待会让整体更慢。
    llm_concurrency: int = 5
    llm_timeout: float = 60.0
    llm_max_retries: int = 3
    # 排序任务要稳定可复现，温度固定为 0
    llm_temperature: float = 0.0

    # ---------- PDF 处理 ----------
    # 页面渲染分辨率。150 足够模型识别正文，再高只是白烧 token
    render_dpi: int = 150
    jpeg_quality: int = 85
    # 文本层字符数超过该阈值 → 走纯文本链路，成本约为多模态的 1/10
    text_layer_min_chars: int = 50

    # ---------- 排序 ----------
    # 排序结果非法（不是全排列）时的最大重试次数
    order_max_attempts: int = 3
    # 整体置信度低于该值时，所有页在客户端标记为「需人工复核」
    low_confidence_threshold: float = 0.7

    @property
    def upload_dir(self) -> Path:
        return self.storage_dir / "uploads"

    @property
    def result_dir(self) -> Path:
        return self.storage_dir / "results"

    def ensure_dirs(self) -> None:
        """确保存储目录存在，启动时调用一次即可。"""
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.result_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
