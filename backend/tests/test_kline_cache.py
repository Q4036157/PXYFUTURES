from app.config import settings
from app.services.repository import ConfigRepository


def test_kline_cache_persists_history_and_updates_the_current_bar(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    repository = ConfigRepository()

    repository.save_kline_bars(
        "DCE.V2609",
        86400,
        [(1, 100.0), (2, 101.0), (3, 102.0)],
        requested_count=300,
        synced_at_ns=1000,
    )
    repository.save_kline_bars(
        "DCE.V2609",
        86400,
        [(3, 103.5), (4, 104.0)],
        requested_count=10,
        synced_at_ns=2000,
    )

    assert repository.load_kline_bars("DCE.V2609", 86400, 10) == [
        (1, 100.0),
        (2, 101.0),
        (3, 103.5),
        (4, 104.0),
    ]
    state = repository.get_kline_sync_state("DCE.V2609", 86400)
    assert state is not None
    assert state.max_requested_bars == 300
    assert state.last_synced_at_ns == 2000
