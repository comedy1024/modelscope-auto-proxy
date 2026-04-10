"""
配置模块 — 从 .env 加载所有运行参数
"""
import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ModelScope API
    modelscope_api_key: str = ""
    modelscope_base_url: str = "https://api-inference.modelscope.cn/v1"

    # 代理服务
    proxy_port: int = 8000
    virtual_model_name: str = "modelscope-auto"

    # 模型筛选
    min_param_b: int = 4

    # 模型刷新间隔（秒）
    model_refresh_interval: int = 86400

    # 日志
    log_level: str = "INFO"

    # 数据目录
    data_dir: Path = Path(__file__).parent / "data"
    log_dir: Path = Path(__file__).parent / "logs"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
