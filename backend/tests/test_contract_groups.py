from app.config import settings
from app.services.repository import ConfigRepository


def test_existing_contracts_are_migrated_into_default_group_and_can_be_reordered(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    repository = ConfigRepository()

    first = repository.create_contract("user-1", "CZCE.FG609", "FG609")
    second = repository.create_contract("user-1", "DCE.PP2701", "PP2701")

    groups = repository.list_contract_groups("user-1")
    assert [group.name for group in groups] == ["自定义1", "自定义2", "自定义3"]
    assert [contract.id for contract in groups[0].contracts] == [first.id, second.id]

    assert repository.reorder_contracts("user-1", groups[0].id, [second.id, first.id]) is True
    assert [contract.id for contract in repository.list_contract_groups("user-1")[0].contracts] == [
        second.id,
        first.id,
    ]


def test_contract_can_move_between_groups_and_new_group_is_persisted(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    repository = ConfigRepository()

    contract = repository.create_contract("user-2", "DCE.PP2701", "PP2701")
    custom = repository.create_group("user-2", "夜盘")

    moved = repository.move_contract("user-2", contract.id, custom.id)
    assert moved is not None
    assert moved.group_id == custom.id
    assert [group.name for group in repository.list_contract_groups("user-2")] == [
        "自定义1",
        "自定义2",
        "自定义3",
        "夜盘",
    ]
    assert [item.id for item in repository.list_contract_groups("user-2")[-1].contracts] == [contract.id]
