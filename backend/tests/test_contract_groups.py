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


def test_added_order_manual_order_and_append_survive_reload_and_rename(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    repository = ConfigRepository()
    first = repository.create_contract("order-user", "CZCE.SR701", "SR701")
    second = repository.create_contract("order-user", "CZCE.AP701", "AP701")
    group_id = repository.list_contract_groups("order-user")[0].id
    assert [item.id for item in repository.list_contracts("order-user")] == [first.id, second.id]
    assert repository.reorder_contracts("order-user", group_id, [second.id, first.id])
    third = repository.create_contract("order-user", "CZCE.FG701", "FG701")
    reloaded = ConfigRepository()
    renamed = reloaded.rename_group("order-user", group_id, "AA")
    assert renamed is not None
    assert [item.id for item in renamed.contracts] == [second.id, first.id, third.id]
    assert [item.id for item in reloaded.list_contract_groups("order-user")[0].contracts] == [
        second.id, first.id, third.id
    ]
    assert not reloaded.reorder_contracts("order-user", group_id, [first.id, first.id, third.id])
    assert not reloaded.reorder_contracts("another-user", group_id, [second.id, first.id, third.id])
    assert [item.id for item in reloaded.list_contracts("order-user")] == [
        second.id, first.id, third.id
    ]


def test_equal_sort_values_and_unassigned_contracts_use_added_order(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    repository = ConfigRepository()
    first = repository.create_contract("legacy-user", "CZCE.SR701", "SR701")
    second = repository.create_contract("legacy-user", "CZCE.AP701", "AP701")
    with repository._connect() as connection:
        connection.execute("UPDATE contracts SET sort_order = 0 WHERE user_id = ?", ("legacy-user",))
    assert [item.id for item in repository.list_contracts("legacy-user")] == [first.id, second.id]
    assert [item.id for item in repository.list_contract_groups("legacy-user")[0].contracts] == [
        first.id, second.id
    ]
    with repository._connect() as connection:
        connection.execute("UPDATE contracts SET group_id = NULL WHERE user_id = ?", ("legacy-user",))
    assert [item.id for item in repository.list_contract_groups("legacy-user")[0].contracts] == [
        first.id, second.id
    ]
