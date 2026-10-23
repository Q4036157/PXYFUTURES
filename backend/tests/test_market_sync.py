import asyncio

from app.market.tq_client import MarketBars, TqCredentialsInvalid
from app.services.market_sync import (
    build_market_sync_jobs,
    run_market_sync_loop,
    sync_all_market_data,
)
from app.services.repository import ContractConfig, PeriodConfig


class EmptyFollowups:
    @staticmethod
    def list_followup_alerts() -> list[dict[str, object]]:
        return []


def _period(duration_seconds: int, m4: int) -> PeriodConfig:
    return PeriodConfig(
        id=None,
        label=f"{duration_seconds}秒",
        duration_seconds=duration_seconds,
        m4=m4,
        m3=60,
        m2=20,
        m1=5,
    )


def test_build_jobs_includes_all_contracts_and_merges_duplicate_periods() -> None:
    contracts = [
        ContractConfig(1, "DCE.PP2701", "PP2701", [_period(7200, 120), _period(86400, 180)]),
        ContractConfig(2, "DCE.V2609", "PVC2609", [_period(7200, 100)]),
        ContractConfig(3, "DCE.PP2701", "PP2701", [_period(7200, 240)]),
    ]

    jobs = build_market_sync_jobs(contracts)

    assert [(job.symbol, job.duration_seconds, job.count) for job in jobs] == [
        ("DCE.PP2701", 300, 12984),
        ("DCE.PP2701", 86400, 481),
        ("DCE.V2609", 300, 9624),
    ]


def test_build_jobs_deduplicates_periods_that_share_a_base_series() -> None:
    contracts = [
        ContractConfig(
            1,
            "CZCE.UR701",
            "UR701",
            [_period(180, 200), _period(900, 240), _period(1800, 240)],
        )
    ]

    jobs = build_market_sync_jobs(contracts)

    assert [(job.duration_seconds, job.count) for job in jobs] == [
        (60, 1503),
        (300, 3246),
    ]


def test_sync_failure_does_not_block_later_jobs() -> None:
    class Repository(EmptyFollowups):
        @staticmethod
        def list_all_contracts() -> list[ContractConfig]:
            return [
                ContractConfig(1, "DCE.PP2701", "PP2701", [_period(7200, 120)]),
                ContractConfig(2, "DCE.V2609", "PVC2609", [_period(7200, 120)]),
            ]

    class Client:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def fetch_bars(self, symbol: str, *args: object) -> MarketBars:
            self.calls.append(symbol)
            if symbol == "DCE.PP2701":
                raise RuntimeError("模拟行情失败")
            return MarketBars([], "live", None)

    client = Client()
    result = asyncio.run(sync_all_market_data(client, Repository()))  # type: ignore[arg-type]

    assert client.calls == ["DCE.PP2701", "DCE.V2609"]
    assert (result.jobs, result.live, result.cache, result.failed) == (2, 1, 0, 1)


def test_sync_loop_can_stop_after_one_cycle() -> None:
    stop_event = asyncio.Event()

    class Repository(EmptyFollowups):
        @staticmethod
        def list_all_contracts() -> list[ContractConfig]:
            stop_event.set()
            return []

    asyncio.run(
        run_market_sync_loop(  # type: ignore[arg-type]
            object(),
            Repository(),
            stop_event,
            interval_seconds=0.01,
        )
    )


def test_sync_loop_publishes_rollover_without_fixed_sleep() -> None:
    stop_event = asyncio.Event()
    received = []

    class Repository(EmptyFollowups):
        @staticmethod
        def list_all_contracts() -> list[ContractConfig]:
            return [ContractConfig(1, "CZCE.UR701", "UR701", [_period(180, 200)])]

    class Client:
        @staticmethod
        def wait_for_bar_rollovers(*_args: object) -> object:
            return type(
                "Rollover",
                (),
                {
                    "changed_sources": {("CZCE.UR701", 60): 123},
                    "received_at_ns": 456,
                },
            )()

    async def run() -> None:
        async def on_rollover(event: object) -> None:
            received.append(event)
            stop_event.set()

        await run_market_sync_loop(
            Client(),  # type: ignore[arg-type]
            Repository(),  # type: ignore[arg-type]
            stop_event,
            on_rollover=on_rollover,  # type: ignore[arg-type]
        )

    asyncio.run(run())

    assert received[0].changed_sources == {("CZCE.UR701", 60): 123}
    assert received[0].received_at_ns == 456


def test_sync_loop_waits_for_credentials_change_without_retry_storm() -> None:
    stop_event = asyncio.Event()

    class Repository(EmptyFollowups):
        @staticmethod
        def list_all_contracts() -> list[ContractConfig]:
            return [ContractConfig(1, "CZCE.UR701", "UR701", [_period(180, 200)])]

    class Client:
        credential_revision = 0
        calls = 0

        def wait_for_bar_rollovers(self, *_args: object) -> object:
            self.calls += 1
            if self.calls == 1:
                raise TqCredentialsInvalid("天勤账号或密码错误，请重新输入")
            stop_event.set()
            return type("Rollover", (), {"changed_sources": {}, "received_at_ns": 0})()

    client = Client()

    async def run() -> None:
        async def unlock() -> None:
            await asyncio.sleep(0.02)
            client.credential_revision += 1

        unlock_task = asyncio.create_task(unlock())
        await run_market_sync_loop(
            client,  # type: ignore[arg-type]
            Repository(),  # type: ignore[arg-type]
            stop_event,
        )
        await unlock_task

    asyncio.run(run())

    assert client.calls == 2
