"""智能期货运行配置。"""
from __future__ import annotations

import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全部敏感值均从环境变量或本地私有存储读取。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 3022
    log_level: str = "INFO"
    data_dir: Path = Path("./data")
    # 必须与 PXYLH 的 DAA_APP_SESSION_SECRET 相同。
    app_session_secret: str = ""
    tq_user_name: str = ""
    tq_password: str = ""

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "pxyfutures.log"

    def model_post_init(self, __context: object, /) -> None:
        if not self.data_dir.is_absolute():
            base_dir = (
                Path(sys.executable).resolve().parent
                if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parents[2]
            )
            self.data_dir = (base_dir / self.data_dir).resolve()
        # 兼容单独使用 DAA_APP_SESSION_SECRET 环境变量名。
        if not self.app_session_secret.strip():
            import os

            self.app_session_secret = os.getenv("DAA_APP_SESSION_SECRET", "").strip()


settings = Settings()
