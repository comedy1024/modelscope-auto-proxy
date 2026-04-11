"""
配置模块 — 从 .env 加载所有运行参数
"""
import os
import secrets
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
    log_retention_days: int = 30  # 日志保留天数，0 表示永不清空

    # 回复头部模型标识（非 vibe coding 场景）
    show_model_tag: bool = False  # True 时在回复文本开头注入 [模型名]

    # 管理后台认证
    admin_username: str = "admin"
    admin_password: str = ""  # 为空时启动时自动生成随机密码

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

        # 如果未设置密码，自动生成一个
        if not self.admin_password:
            self.admin_password = secrets.token_urlsafe(12)
            # 保存到 .env 以便用户查看
            self._save_generated_password()

    def _save_generated_password(self):
        """将自动生成的密码保存到 .env 文件"""
        env_path = Path(__file__).parent / ".env"

        # 读取现有 .env
        env_lines = []
        if env_path.exists():
            env_lines = env_path.read_text(encoding="utf-8").splitlines()

        # 检查是否已有 ADMIN_PASSWORD 行
        has_admin_password = any(
            line.strip().startswith("ADMIN_PASSWORD=")
            for line in env_lines
            if line.strip() and not line.strip().startswith("#")
        )

        if not has_admin_password:
            env_lines.append(f"ADMIN_PASSWORD={self.admin_password}")
            env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")


settings = Settings()
