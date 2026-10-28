"""全局配置：单一来源，pydantic-settings 驱动，.env 可覆盖。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 功能档位：conservative（默认）= 实验功能全关，行为可预测、可随时回滚；
    # full = 一键开启缓存/指代消解/澄清挂起（演示与个人使用），单开关仍可显式覆盖。
    profile: str = "conservative"  # conservative | full

    # LLM
    llm_provider: str = "mock"  # mock | deepseek | openai_compatible
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    openai_base_url: str = ""
    openai_api_key: str = ""
    openai_model: str = ""

    # 数据
    data_dir: str = "data"
    db_path: str = "data/askbi.duckdb"
    generator_seed: int = 42
    generator_months: int = 24
    generator_end_month: str = "2026-09"

    # 安全
    sql_timeout_seconds: float = 10.0
    role: str = "boss"

    # 编排
    max_sql_repairs: int = 2
    max_qa_revisions: int = 2
    rag_backend: str = "tfidf"
    rag_topk: int = 3

    # 查询缓存（E4③：同 DSL 指纹回放 + TTL/版本双闸；默认关闭，行为与无缓存一致）
    cache_enabled: bool = False
    followup_enabled: bool = False   # 多轮指代消解（E 系原创），默认关闭
    clarify_suspend_enabled: bool = False  # E4②：澄清挂起/恢复（checkpointer interrupt），默认关闭
    cache_ttl_seconds: int = 300
    cache_max_entries: int = 256

    # 服务
    api_key: str = ""

    @property
    def db_abs_path(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else PROJECT_ROOT / p

    def model_post_init(self, __context) -> None:
        """PROFILE=full 时为未显式配置的实验功能补默认开（显式配置永远优先）。

        只翻转"默认关"的三个实验开关，不碰安全/编排参数——
        档位是演示便利，不是绕过配置的旁路。
        """
        if self.profile == "full":
            for field in ("cache_enabled", "followup_enabled", "clarify_suspend_enabled"):
                if field not in self.model_fields_set:
                    setattr(self, field, True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
