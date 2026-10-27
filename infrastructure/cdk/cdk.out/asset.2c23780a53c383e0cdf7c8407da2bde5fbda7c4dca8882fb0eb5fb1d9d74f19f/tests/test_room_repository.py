import asyncio
import time

import pytest
from botocore.exceptions import ClientError

from app.room_manager import (
    Room,
    RoomManager,
    RoomPersistenceError,
)
from app.room_repository import (
    MAX_DYNAMODB_ITEM_BYTES,
    DynamoDBRoomRepository,
    InMemoryRoomRepository,
    RoomSerializationError,
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
