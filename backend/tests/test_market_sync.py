import asyncio

from app.market.tq_client import MarketBars
from app.services.market_sync import build_market_sync_jobs, run_market_sync_loop, sync_all_market_data
from app.services.repository import ContractConfig, PeriodConfig


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
        ("DCE.PP2701", 7200, 541),
        ("DCE.PP2701", 86400, 481),
        ("DCE.V2609", 7200, 401),
    ]


def test_sync_failure_does_not_block_later_jobs() -> None:
    class Repository:
        @staticmethod
        def list_all_contracts() -> list[ContractConfig]:
            return [
                ContractConfig(1, "DCE.PP2701", "PP2701", [_period(7200, 120)]),
                ContractConfig(2, "DCE.V2609", "PVC2609", [_period(7200, 120)]),
            ]

    class Client:
        calls: list[str] = []

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

    class Repository:
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
