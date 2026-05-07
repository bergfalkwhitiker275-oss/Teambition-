"""配置管理模块 - 使用 pydantic-settings 管理所有环境变量"""

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置, 从 .env 文件或环境变量中读取"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 钉钉配置
    dingtalk_app_key: str = ""
    dingtalk_app_secret: str = ""
    dingtalk_robot_webhook: str = ""

    # Teambition 配置
    teambition_app_id: str = ""
    teambition_app_secret: str = ""
    teambition_org_id: str = ""
    teambition_default_project_id: str = ""
    teambition_default_view_id: str = ""
    teambition_project_key: str = ""

    # LLM 配置
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"

    # LLM 备用模型配置 (主模型配额耗尽时自动切换)
    llm_fallback_api_key: str = ""
    llm_fallback_base_url: str = ""
    llm_fallback_model: str = ""
    llm_fallback_provider: str = "openai"  # "openai" 或 "anthropic"


@lru_cache
def get_settings() -> Settings:
    """获取全局配置单例"""
    return Settings()
