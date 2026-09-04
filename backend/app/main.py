"""智能期货 FastAPI 入口。"""
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth, contracts, signals
from app.api import settings as settings_api
from app.config import settings
from app.market.tq_client import TqClient
from app.services.market_sync import run_market_sync_loop
from app.services.repository import ConfigRepository

settings.log_dir.mkdir(parents=True, exist_ok=True)
_log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_file_handler = logging.FileHandler(settings.log_file, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter(_log_format))
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(logging.Formatter(_log_format))
logging.basicConfig(
    level=settings.log_level,
    handlers=[_file_handler, _stream_handler],
    force=True,
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.config_repository = ConfigRepository()
    app.state.tq_client = TqClient()
    app.state.market_sync_stop = asyncio.Event()
    app.state.market_sync_task = asyncio.create_task(
        run_market_sync_loop(
            app.state.tq_client,
            app.state.config_repository,
            app.state.market_sync_stop,
        ),
        name="market-data-sync",
    )
    from app.services import auth as auth_service
    logger.info("智能期货启动，认证模式：%s", auth_service.auth_mode())
    try:
        yield
    finally:
        app.state.market_sync_stop.set()
        await app.state.market_sync_task
        app.state.tq_client.close()


app = FastAPI(title="貔貅元智能期货", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth.router)
app.include_router(contracts.router)
app.include_router(signals.router)
app.include_router(settings_api.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "pxyfutures"}


# 冻结版从 PyInstaller 解压目录读取资源；开发版仍读取项目中的构建产物。
_bundle_root = getattr(sys, "_MEIPASS", None)
_static_dir = (
    Path(_bundle_root) / "frontend" / "dist"
    if _bundle_root
    else Path(__file__).resolve().parents[2] / "frontend" / "dist"
)
if _static_dir.exists():
    if (_static_dir / "assets").exists():
        app.mount("/assets", StaticFiles(directory=_static_dir / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        return FileResponse(_static_dir / "index.html", headers={"Cache-Control": "no-store"})
