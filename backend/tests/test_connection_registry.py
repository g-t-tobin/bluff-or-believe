import threading

import pytest

from app.room_manager import RoomManager
from app.room_repository import InMemoryRoomRepository, RoomWriteOutcomeUnknown


class HookedRepository(InMemoryRoomRepository):
    """In-memory repository with injectable save/load behaviour."""

    def __init__(self) -> None:
        super().__init__()
        self.before_save = None
        self.after_save = None

    def save(self, room, expected_version):
        if self.before_save is not None:
            hook, self.before_save = self.before_save, None
            hook()
        saved = super().save(room, expected_version)
        if self.after_save is not None:
            hook, self.after_save = self.after_save, None
            hook()
        return saved


def make_manager() -> tuple[RoomManager, HookedRepository]:
    repository = HookedRepository()
    return RoomManager(repository), repository


def test_connection_registered_during_mutation_can_be_attached() -> None:
    manager, repository = make_manager()
    room = manager.create_room("Race")
    player = manager.join_room(room.id, "Avery").player

    saving = threading.Event()
    connector_started = threading.Event()
    release_save = threading.Event()
    results: dict[str, object] = {}

    def block_save() -> None:
        saving.set()
        assert release_save.wait(timeout=5)

    def connect_then_attach() -> None:
        connector_started.set()
        try:
            connection_id = manager.connect_socket(room.id, object())
            manager.attach_player_to_connection(room.id, connection_id, player.id)
            results["connection_id"] = connection_id
        except Exception as exc:  # reported to the main thread
            results["error"] = exc

    repository.before_save = block_save
    mutator = threading.Thread(target=lambda: manager.set_ready(room.id, player.id, True))
    mutator.start()
    assert saving.wait(timeout=5)
    connector = threading.Thread(target=connect_then_attach)
    connector.start()
    assert connector_started.wait(timeout=5)
    release_save.set()
    mutator.join(timeout=5)
    connector.join(timeout=5)

    assert "error" not in results
    cached = manager.get_cached_room(room.id)
    assert cached is not None
    assert list(cached.connections) == [results["connection_id"]]
    assert cached.players[player.id].ready is True


def test_connection_registered_before_mutation_survives_successful_save() -> None:
    manager, _ = make_manager()
    room = manager.create_room("Success")
    player = manager.join_room(room.id, "Avery").player
    connection_id = manager.connect_socket(room.id, object())

    manager.set_ready(room.id, player.id, True)

    manager.attach_player_to_connection(room.id, connection_id, player.id)
    assert connection_id in manager.get_cached_room(room.id).connections


def test_connection_added_mid_save_survives_cache_replacement() -> None:
    manager, repository = make_manager()
    room = manager.create_room("Mid save")
    player = manager.join_room(room.id, "Avery").player
    holder: dict[str, str] = {}

    # Same thread re-enters the RLock, emulating a registration landing between
    # candidate creation and cache replacement.
    repository.before_save = lambda: holder.update(
        id=manager.connect_socket(room.id, object())
    )
    manager.set_ready(room.id, player.id, True)

    manager.attach_player_to_connection(room.id, holder["id"], player.id)


@pytest.mark.parametrize("committed", [True, False])
def test_connection_survives_ambiguous_write_recovery(committed: bool) -> None:
    manager, repository = make_manager()
    room = manager.create_room("Ambiguous")
    player = manager.join_room(room.id, "Avery").player
    connection_id = manager.connect_socket(room.id, object())
    original_save = InMemoryRoomRepository.save
    calls = {"n": 0}

    def flaky_save(room_obj, expected_version):
        calls["n"] += 1
        if calls["n"] == 1:
            if committed:
                original_save(repository, room_obj, expected_version)
            raise RoomWriteOutcomeUnknown("simulated lost response")
        return original_save(repository, room_obj, expected_version)

    repository.save = flaky_save
    manager.set_ready(room.id, player.id, True)

    assert calls["n"] == (1 if committed else 2)
    manager.attach_player_to_connection(room.id, connection_id, player.id)
    assert manager.get_cached_room(room.id).players[player.id].ready is True


def test_idempotent_room_creation_replay_preserves_connections() -> None:
    manager, _ = make_manager()
    room = manager.create_room("Replay", operation_id="a" * 32)
    connection_id = manager.connect_socket(room.id, object())

    manager.create_room("Replay", operation_id="a" * 32)

    assert connection_id in manager.get_cached_room(room.id).connections


def test_reload_from_repository_preserves_connections() -> None:
    manager, _ = make_manager()
    room = manager.create_room("Reload")
    connection_id = manager.connect_socket(room.id, object())

    manager.get_room(room.id)

    assert connection_id in manager.get_cached_room(room.id).connections


def test_reconnecting_player_leaves_single_active_connection() -> None:
    manager, _ = make_manager()
    room = manager.create_room("Single")
    player = manager.join_room(room.id, "Avery").player
    first = manager.connect_socket(room.id, object())
    manager.attach_player_to_connection(room.id, first, player.id)
    manager.set_ready(room.id, player.id, True)
    second = manager.connect_socket(room.id, object())
    manager.attach_player_to_connection(room.id, second, player.id)

    assert list(manager.get_cached_room(room.id).connections) == [second]


def test_concurrent_connects_and_mutations_never_lose_a_connection() -> None:
    manager, _ = make_manager()
    room = manager.create_room("Stress")
    player = manager.join_room(room.id, "Avery").player
    workers = 8
    barrier = threading.Barrier(workers * 2)
    errors: list[Exception] = []
    ids: list[str] = []

    def connector() -> None:
        barrier.wait(timeout=5)
        try:
            connection_id = manager.connect_socket(room.id, object())
            manager.attach_player_to_connection(room.id, connection_id, player.id)
            ids.append(connection_id)
        except Exception as exc:
            errors.append(exc)

    def mutator(index: int) -> None:
        barrier.wait(timeout=5)
        try:
            manager.set_ready(room.id, player.id, index % 2 == 0)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=connector) for _ in range(workers)] + [
        threading.Thread(target=mutator, args=(i,)) for i in range(workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert len(ids) == workers
    connections = manager.get_cached_room(room.id).connections
    assert len(connections) == 1  # latest attach evicts earlier ones for the same player
    assert next(iter(connections)) in ids
