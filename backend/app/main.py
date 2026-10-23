"""智能期货 FastAPI 入口。"""
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import auth, contracts, followup_alerts, signals
from app.api import settings as settings_api
from app.config import settings
from app.market.tq_client import TqClient
from app.services.alert_stream import AlertStreamService
from app.services.followup_alerts import FollowupAlertService
from app.services.market_sync import MarketSyncEvent, run_market_sync_loop
from app.services.repository import ConfigRepository
from app.version import APP_VERSION

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
    app.state.alert_stream = AlertStreamService(
        app.state.config_repository,
        partial(signals.scan_cached_alerts_for_user, client=app.state.tq_client),
    )
    app.state.followup_alerts = FollowupAlertService(
        app.state.config_repository,
        lambda repo, jobs, cursors: signals._scan_cached_alert_jobs(repo, jobs, replay_since=cursors),
        app.state.tq_client.contract_status,
    )
    app.state.market_sync_stop = asyncio.Event()
    app.state.followup_alert_task = asyncio.create_task(
        app.state.followup_alerts.run(app.state.market_sync_stop), name="followup-alerts",
    )

    async def publish_rollovers(event: MarketSyncEvent) -> None:
        await app.state.alert_stream.publish_rollovers(event)
        await app.state.followup_alerts.publish_rollovers(event)

    app.state.market_sync_task = asyncio.create_task(
        run_market_sync_loop(
            app.state.tq_client,
            app.state.config_repository,
            app.state.market_sync_stop,
            on_rollover=publish_rollovers,
        ),
        name="market-data-sync",
    )
    from app.services import auth as auth_service
    logger.info(
        "智能期货启动，版本：%s，认证模式：%s",
        APP_VERSION,
        auth_service.auth_mode(),
    )
    try:
        yield
    finally:
        app.state.market_sync_stop.set()
        await app.state.market_sync_task
        app.state.followup_alerts.wake()
        await app.state.followup_alert_task
        await app.state.alert_stream.close()
        app.state.tq_client.close()


app = FastAPI(title="貔貅元智能期货", version=APP_VERSION, lifespan=lifespan)
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
app.include_router(followup_alerts.router)
app.include_router(settings_api.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "pxyfutures", "version": APP_VERSION}


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
