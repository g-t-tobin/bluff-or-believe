import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from app.main import create_room_manager
from app.room_manager import (
    Room,
    RoomManager,
    RoomOutcomeUncertainError,
    RoomPersistenceError,
)
from app.room_repository import (
    MAX_DYNAMODB_ITEM_BYTES,
    DynamoDBRoomRepository,
    InMemoryRoomRepository,
    RoomSerializationError,
    RoomWriteOutcomeUnknown,
    room_to_item,
)


def test_room_state_round_trips_and_restores_session_and_active_game() -> None:
    repository = InMemoryRoomRepository()
    first_manager = RoomManager(repository)
    room = first_manager.create_room("Restore Table")
    first_join = first_manager.join_room(room.id, "Avery")
    second_join = first_manager.join_room(room.id, "Blake")
    first_manager.join_room(room.id, "Casey")
    first_manager.set_ready(room.id, first_join.player.id, True)
    first_manager.start_round(room.id)
    first_manager.cast_vote(room.id, second_join.player.id, True)

    restarted_manager = RoomManager(repository)
    restored = restarted_manager.get_room(room.id)

    assert restored is not None
    assert restored.version == room.version + 6
    assert restored.players[first_join.player.id].ready is True
    assert restored.players[first_join.player.id].session_digest == first_join.player.session_digest
    assert (
        restored.players[first_join.player.id].session_expires_at
        == first_join.player.session_expires_at
    )
    assert restarted_manager.authenticate_session(room.id, first_join.session_token) is not None
    assert restored.game is not None
    assert restored.game.votes == {second_join.player.id: True}
    assert restored.scores[first_join.player.id] == 0


def test_local_configuration_uses_memory_even_with_table_name_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("ROOMS_TABLE_NAME", "not-used-by-local-tests")

    manager = create_room_manager()

    assert isinstance(manager.repository, InMemoryRoomRepository)


def test_expired_room_is_rejected_before_ttl_deletion() -> None:
    repository = InMemoryRoomRepository()
    manager = RoomManager(repository)
    room = manager.create_room("Expired Table")
    room.expires_at = int(time.time()) - 1
    assert repository.save(room, expected_version=room.version)

    assert manager.get_room(room.id) is None
    with pytest.raises(ValueError, match="Unknown room"):
        manager.join_room(room.id, "Avery")


def test_reads_chat_and_disconnect_do_not_extend_room_expiry() -> None:
    repository = InMemoryRoomRepository()
    manager = RoomManager(repository)
    room = manager.create_room("No Read Extension")
    joined = manager.join_room(room.id, "Avery")
    room = manager.get_room(room.id)
    assert room is not None
    room.expires_at = int(time.time()) + 600
    assert repository.save(room, expected_version=room.version)
    original_expiry = room.expires_at

    assert manager.get_room(room.id) is not None
    assert manager.room_summary(room.id)["room_id"] == room.id
    assert manager.authenticate_session(room.id, joined.session_token) is not None
    connection_id = manager.connect_socket(room.id, object())
    manager.attach_player_to_connection(room.id, connection_id, joined.player.id)
    asyncio.run(manager.broadcast_room_message(room.id, "Avery", "hello"))
    manager.disconnect_socket(room.id, connection_id)

    persisted = manager.get_room(room.id)
    assert persisted is not None
    assert persisted.expires_at == original_expiry


def test_optimistic_conflict_reloads_and_revalidates_duplicate_join() -> None:
    underlying = InMemoryRoomRepository()
    competitor = RoomManager(underlying)
    room = competitor.create_room("Concurrent Join")

    class ConflictingRepository:
        conflict_once = True

        def load(self, room_id):
            return underlying.load(room_id)

        def save(self, candidate, expected_version):
            if expected_version is not None and self.conflict_once:
                self.conflict_once = False
                competitor.join_room(room.id, "Avery")
            return underlying.save(candidate, expected_version)

    racing_manager = RoomManager(ConflictingRepository())
    with pytest.raises(ValueError, match="already in use"):
        racing_manager.join_room(room.id, "Avery")

    persisted = underlying.load(room.id)
    assert persisted is not None
    assert [player.name for player in persisted.players.values()] == ["Avery"]


def test_persistence_failure_does_not_publish_mutation() -> None:
    class FailingRepository(InMemoryRoomRepository):
        fail_writes = False

        def save(self, room, expected_version):
            if self.fail_writes:
                raise RuntimeError("simulated storage outage")
            return super().save(room, expected_version)

    repository = FailingRepository()
    manager = RoomManager(repository)
    room = manager.create_room("Unavailable Storage")
    repository.fail_writes = True

    with pytest.raises(RoomPersistenceError):
        manager.join_room(room.id, "Avery")

    repository.fail_writes = False
    restored = manager.get_room(room.id)
    assert restored is not None
    assert restored.players == {}
    assert restored.scores == {}


def test_unreconciled_write_returns_explicit_uncertain_outcome() -> None:
    class UnavailableAfterWriteRepository(InMemoryRoomRepository):
        def __init__(self):
            super().__init__()
            self.fail_reconciliation = False
            self.timeout_next_write = False

        def save(self, room, expected_version):
            if self.timeout_next_write:
                self.timeout_next_write = False
                self.fail_reconciliation = True
                raise RoomWriteOutcomeUnknown("simulated lost response")
            return super().save(room, expected_version)

        def load(self, room_id):
            if self.fail_reconciliation:
                raise RuntimeError("simulated reconciliation read failure")
            return super().load(room_id)

    repository = UnavailableAfterWriteRepository()
    manager = RoomManager(repository)
    room = manager.create_room("Uncertain Result")
    repository.timeout_next_write = True

    with pytest.raises(RoomOutcomeUncertainError, match="outcome is uncertain"):
        manager.join_room(room.id, "Avery")

    repository.fail_reconciliation = False
    latest = manager.get_room(room.id)
    assert latest is not None
    assert latest.players == {}


def test_stale_version_cannot_overwrite_a_newer_room() -> None:
    repository = InMemoryRoomRepository()
    manager = RoomManager(repository)
    room = manager.create_room("Version Guard")
    stale = repository.load(room.id)
    assert stale is not None

    manager.join_room(room.id, "Avery")

    assert repository.save(stale, expected_version=stale.version) is False


def test_dynamodb_repository_round_trip_and_conditional_version_write() -> None:
    class FakeDynamoClient:
        item = None
        timeout_after_commit = False

        def get_item(self, **_kwargs):
            return {"Item": self.item} if self.item is not None else {}

        def put_item(self, **kwargs):
            expected_condition = kwargs["ConditionExpression"]
            if expected_condition == "attribute_not_exists(#room_id)":
                if self.item is not None:
                    self._conditional_failure()
            elif expected_condition == "#version = :expected_version":
                expected_version = int(
                    kwargs["ExpressionAttributeValues"][":expected_version"]["N"]
                )
                current_version = int(self.item["version"]["N"])
                if expected_version != current_version:
                    self._conditional_failure()
            self.item = kwargs["Item"]
            if self.timeout_after_commit:
                self.timeout_after_commit = False
                raise ReadTimeoutError(
                    endpoint_url="https://dynamodb.eu-west-1.amazonaws.com",
                    error="response timed out",
                )

        @staticmethod
        def _conditional_failure():
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}},
                "PutItem",
            )

    client = FakeDynamoClient()
    repository = DynamoDBRoomRepository(
        "BluffOrBelieveRooms",
        client=client,
    )
    manager = RoomManager(repository)
    room = manager.create_room("Dynamo Adapter")
    manager.join_room(room.id, "Avery")

    restored = repository.load(room.id)
    assert restored is not None
    assert restored.version == 2
    assert restored.players[next(iter(restored.players))].name == "Avery"
    assert repository.save(restored, expected_version=1) is False


def test_dynamodb_write_timeout_after_commit_is_reconciled_without_duplicate_score() -> None:
    class FakeDynamoClient:
        item = None
        timeout_after_commit = False

        def get_item(self, **_kwargs):
            return {"Item": self.item} if self.item is not None else {}

        def put_item(self, **kwargs):
            if self.item is None:
                assert kwargs["ConditionExpression"] == "attribute_not_exists(#room_id)"
            else:
                expected = int(kwargs["ExpressionAttributeValues"][":expected_version"]["N"])
                if expected != int(self.item["version"]["N"]):
                    raise ClientError(
                        {"Error": {"Code": "ConditionalCheckFailedException"}},
                        "PutItem",
                    )
            self.item = kwargs["Item"]
            if self.timeout_after_commit:
                self.timeout_after_commit = False
                raise ReadTimeoutError(
                    endpoint_url="https://dynamodb.eu-west-1.amazonaws.com",
                    error="response timed out",
                )

    client = FakeDynamoClient()
    repository = DynamoDBRoomRepository("BluffOrBelieveRooms", client=client)
    manager = RoomManager(repository)
    room = manager.create_room("Ambiguous Commit")
    speaker = manager.join_room(room.id, "Avery").player
    voter = manager.join_room(room.id, "Blake").player
    round_state = manager.start_round(room.id)
    assert round_state.speaker_id == speaker.id
    client.timeout_after_commit = True

    committed_round = manager.cast_vote(room.id, voter.id, round_state.truth_value)

    assert committed_round.resolved is True
    committed_room = manager.get_room(room.id)
    assert committed_room is not None
    committed_scores = dict(committed_room.scores)
    assert sum(committed_scores.values()) == 4
    with pytest.raises(ValueError, match="already voted"):
        manager.cast_vote(room.id, voter.id, not round_state.truth_value)
    final_room = manager.get_room(room.id)
    assert final_room is not None
    assert final_room.scores == committed_scores


def test_dynamodb_repository_marks_unconfirmed_transport_outcome_ambiguous() -> None:
    class TimedOutClient:
        def put_item(self, **_kwargs):
            raise ReadTimeoutError(
                endpoint_url="https://dynamodb.eu-west-1.amazonaws.com",
                error="response timed out",
            )

    repository = DynamoDBRoomRepository("BluffOrBelieveRooms", client=TimedOutClient())
    room = Room(id="timeout", name="Timeout")

    with pytest.raises(RoomWriteOutcomeUnknown):
        repository.save(room, expected_version=None)


def test_serialized_room_excludes_raw_tokens_and_live_connections() -> None:
    manager = RoomManager()
    room = manager.create_room("Serialization Boundary")
    joined = manager.join_room(room.id, "Avery")
    room = manager.get_room(room.id)
    assert room is not None
    room.connections["live-connection"] = {"websocket": object(), "player_id": joined.player.id}

    item = room_to_item(room)
    serialized = json.dumps(item)

    assert joined.session_token not in serialized
    assert "websocket" not in serialized
    assert "live-connection" not in serialized
    assert joined.player.session_digest in serialized


def test_active_and_resolved_privacy_survives_room_deserialization() -> None:
    repository = InMemoryRoomRepository()
    first_manager = RoomManager(repository)
    room = first_manager.create_room("Restored Privacy")
    players = [
        first_manager.join_room(room.id, name).player for name in ("Avery", "Blake", "Casey")
    ]
    first_manager.start_round(room.id)
    speaker_id = players[0].id
    # Player order makes the first player the opening speaker.
    first_manager.cast_vote(room.id, players[1].id, True)

    active_view = RoomManager(repository).room_summary(room.id, players[1].id)["game"]
    assert active_view["has_voted"] is True
    assert "truth_value" not in active_view
    assert "votes" not in active_view

    first_manager.cast_vote(room.id, players[2].id, False)
    restored_manager = RoomManager(repository)
    resolved_view = restored_manager.room_summary(room.id, speaker_id)["game"]
    assert resolved_view["resolved"] is True
    assert isinstance(resolved_view["truth_value"], bool)
    assert resolved_view["votes"] == {players[1].id: True, players[2].id: False}


class BarrierRepository:
    def __init__(self, repository: InMemoryRoomRepository) -> None:
        self.repository = repository
        self.barrier: threading.Barrier | None = None
        self._barrier_calls = 0
        self._lock = threading.Lock()

    def load(self, room_id):
        return self.repository.load(room_id)

    def save(self, room, expected_version):
        with self._lock:
            barrier = (
                self.barrier if expected_version is not None and self._barrier_calls < 2 else None
            )
            if barrier is not None:
                self._barrier_calls += 1
        if barrier is not None:
            barrier.wait(timeout=5)
        return self.repository.save(room, expected_version)

    def start_race(self) -> None:
        self._barrier_calls = 0
        self.barrier = threading.Barrier(2)


def test_concurrent_competing_votes_revalidate_and_resolve_once() -> None:
    repository = InMemoryRoomRepository()
    setup = RoomManager(repository)
    room = setup.create_room("Concurrent Votes")
    players = [setup.join_room(room.id, name).player for name in ("Avery", "Blake", "Casey")]
    round_state = setup.start_round(room.id)
    assert round_state.speaker_id == players[0].id

    racing_repository = BarrierRepository(repository)
    managers = [RoomManager(racing_repository), RoomManager(racing_repository)]
    racing_repository.start_race()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda args: args[0].cast_vote(room.id, args[1].id, args[2]),
                (
                    (managers[0], players[1], True),
                    (managers[1], players[2], False),
                ),
            )
        )

    assert any(not result.resolved for result in results)
    final_room = setup.get_room(room.id)
    assert final_room is not None and final_room.game is not None
    assert set(final_room.game.votes) == {players[1].id, players[2].id}
    expected_correct = sum(
        vote == final_room.game.truth_value for vote in final_room.game.votes.values()
    )
    expected_score_total = expected_correct * 4
    assert sum(final_room.scores.values()) == expected_score_total


def test_concurrent_round_starts_create_only_one_next_round() -> None:
    repository = InMemoryRoomRepository()
    setup = RoomManager(repository)
    room = setup.create_room("Concurrent Starts")
    players = [setup.join_room(room.id, name).player for name in ("Avery", "Blake")]
    round_state = setup.start_round(room.id)
    voter_id = next(player.id for player in players if player.id != round_state.speaker_id)
    setup.cast_vote(room.id, voter_id, round_state.truth_value)

    racing_repository = BarrierRepository(repository)
    managers = [RoomManager(racing_repository), RoomManager(racing_repository)]
    racing_repository.start_race()

    def start_result(manager):
        try:
            return manager.start_round(room.id)
        except ValueError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(start_result, manager) for manager in managers]
        results = [future.result(timeout=5) for future in futures]

    assert sum(isinstance(result, ValueError) for result in results) == 1
    final_room = setup.get_room(room.id)
    assert final_room is not None and final_room.game is not None
    assert final_room.game.round_number == 2


def test_concurrent_readiness_updates_do_not_lose_a_version() -> None:
    repository = InMemoryRoomRepository()
    setup = RoomManager(repository)
    room = setup.create_room("Concurrent Ready")
    player = setup.join_room(room.id, "Avery").player
    before_version = setup.get_room(room.id).version

    racing_repository = BarrierRepository(repository)
    managers = [RoomManager(racing_repository), RoomManager(racing_repository)]
    racing_repository.start_race()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(managers[0].set_ready, room.id, player.id, True),
            pool.submit(managers[1].set_ready, room.id, player.id, False),
        ]
        [future.result(timeout=5) for future in futures]

    final_room = setup.get_room(room.id)
    assert final_room is not None
    assert final_room.version == before_version + 2
    assert isinstance(final_room.players[player.id].ready, bool)


def test_oversized_room_item_is_rejected_safely() -> None:
    room = Room(id="oversized", name="x" * MAX_DYNAMODB_ITEM_BYTES)

    with pytest.raises(RoomSerializationError, match="400 KB"):
        room_to_item(room)


def test_duplicate_vote_cannot_score_twice_after_restoration() -> None:
    repository = InMemoryRoomRepository()
    manager = RoomManager(repository)
    room = manager.create_room("Score Once")
    manager.join_room(room.id, "Avery")
    voter = manager.join_room(room.id, "Blake").player
    manager.start_round(room.id)

    restarted = RoomManager(repository)
    restarted.cast_vote(room.id, voter.id, True)
    resolved = restarted.get_room(room.id)
    assert resolved is not None
    scores_after_first_vote = dict(resolved.scores)

    with pytest.raises(ValueError, match="already voted"):
        manager.cast_vote(room.id, voter.id, False)

    final = manager.get_room(room.id)
    assert final is not None
    assert final.scores == scores_after_first_vote
    assert final.game is not None and final.game.resolved

    restarted_again = RoomManager(repository)
    restored_final = restarted_again.get_room(room.id)
    assert restored_final is not None and restored_final.game is not None
    assert restored_final.game.resolved is True
    assert restored_final.game.truth_value == final.game.truth_value
    assert restored_final.game.votes == final.game.votes
    assert restored_final.scores == scores_after_first_vote
