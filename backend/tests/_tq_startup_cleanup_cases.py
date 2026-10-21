"""使用真实 SDK 离线验证连接初始化失败时的任务释放。"""

import asyncio
import sys
from types import ModuleType

import pytest
from tqsdk import TqApi

from app.config import settings
from app.market.tq_client import MarketDataUnavailable, TqClient


@pytest.fixture
def offline_client(monkeypatch, tmp_path):
    monkeypatch.setitem(
        sys.modules, "tqsdk.changelog_notification", ModuleType("tqsdk.changelog_notification"),
    )
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr("app.market.tq_client.get_tq_credentials", lambda: ("test", "test"))
    monkeypatch.setattr(TqClient, "validate_credentials", staticmethod(lambda *_args: None))
    now = [100.0]
    monkeypatch.setattr("app.market.tq_client.monotonic", lambda: now[0])
    client = TqClient()
    yield client, now
    client.close()


def test_setup_failure_closes_real_sdk_tasks_and_retry_recovers(monkeypatch, offline_client):
    client, now = offline_client
    failed = []
    error = ConnectionError("离线注入连接失败")

    def fail(api):
        failed.append((api, list(asyncio.all_tasks(api._loop))))
        raise error

    monkeypatch.setattr(TqApi, "_setup_connection", fail)
    with pytest.raises(MarketDataUnavailable) as caught:
        client._get_api()

    api, tasks = failed[0]
    assert caught.value.__cause__ is error
    assert tasks and all(task.done() for task in tasks)
    assert api._loop.is_closed()
    assert client._api is None and client._credential_error is None
    with pytest.raises(MarketDataUnavailable):
        client._get_api()
    assert len(failed) == 1

    def ready(api):
        api._data["mdhis_more_data"] = False
        api._account._get_trade_more_data = lambda _data: False

    monkeypatch.setattr(TqApi, "_setup_connection", ready)
    monkeypatch.setattr(TqApi, "wait_update", lambda *_args, **_kwargs: True)
    now[0] += 3.1
    recovered = client._get_api()
    assert recovered is client._get_api()
    assert not recovered._loop.is_closed()
    assert client._connect_failures == 0 and client._connect_retry_after == 0
    active_tasks = list(asyncio.all_tasks(recovered._loop))
    assert active_tasks and not all(task.done() for task in active_tasks)
    client.close()
    assert recovered._loop.is_closed() and all(task.done() for task in active_tasks)


def test_cleanup_failure_preserves_original_error_without_sensitive_log(
    monkeypatch, offline_client, caplog,
):
    client, _now = offline_client
    captured = []
    error = TimeoutError("离线注入连接超时")
    original_close = TqApi.close

    def fail(api):
        captured.append(api)
        raise error

    def close_fails(_api):
        raise RuntimeError("不要写入日志的认证参数")

    monkeypatch.setattr(TqApi, "_setup_connection", fail)
    monkeypatch.setattr(TqApi, "close", close_fails)
    try:
        with pytest.raises(MarketDataUnavailable) as caught:
            client._get_api()
        assert caught.value.__cause__ is error
        assert "资源释放异常: 类型=RuntimeError" in caplog.text
        assert "不要写入日志的认证参数" not in caplog.text
        assert client._api is None and client._credential_error is None
    finally:
        for api in captured:
            original_close(api)


def test_wait_update_failure_keeps_sdk_cleanup_once(monkeypatch, offline_client):
    client, _now = offline_client
    captured = []
    closed = []
    original_close = TqApi.close
    error = TimeoutError("离线注入截面数据超时")

    def setup(api):
        captured.append((api, list(asyncio.all_tasks(api._loop))))

    def wait_fails(*_args, **_kwargs):
        raise error

    def close(api):
        closed.append(api)
        original_close(api)

    monkeypatch.setattr(TqApi, "_setup_connection", setup)
    monkeypatch.setattr(TqApi, "wait_update", wait_fails)
    monkeypatch.setattr(TqApi, "close", close)
    with pytest.raises(MarketDataUnavailable) as caught:
        client._get_api()
    api, tasks = captured[0]
    assert caught.value.__cause__ is error
    assert closed == [api]
    assert api._loop.is_closed() and all(task.done() for task in tasks)
