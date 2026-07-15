"""智能期货运行配置。"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全部敏感值均从环境变量或本地私有存储读取。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 3022
    log_level: str = "INFO"
    data_dir: Path = Path("./data")
    jwt_secret: str = ""
    tq_user_name: str = ""
    tq_password: str = ""

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "pxyfutures.log"

    def model_post_init(self, __context: object) -> None:
        if not self.data_dir.is_absolute():
            self.data_dir = (Path(__file__).resolve().parents[2] / self.data_dir).resolve()


settings = Settings()
