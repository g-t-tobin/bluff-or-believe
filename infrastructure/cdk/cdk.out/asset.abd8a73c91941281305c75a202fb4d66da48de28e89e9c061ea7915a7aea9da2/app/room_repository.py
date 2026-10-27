from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING, Protocol

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

if TYPE_CHECKING:
    from app.room_manager import Room

SCHEMA_VERSION = 1
MAX_DYNAMODB_ITEM_BYTES = 400 * 1024
MAX_OPERATION_RECEIPTS = 128


class RoomRepository(Protocol):
    def load(self, room_id: str) -> Room | None: ...

    def save(self, room: Room, expected_version: int | None) -> bool: ...


class RoomRepositoryError(RuntimeError):
    """Raised when durable room state cannot be read or written."""


class RoomSerializationError(RoomRepositoryError):
    """Raised when room state cannot safely fit in a DynamoDB item."""


class RoomWriteOutcomeUnknown(RoomRepositoryError):
    """Raised when a write may have committed but its response was not received."""


def room_to_state(room: Room) -> dict[str, object]:
    return {
        "id": room.id,
        "name": room.name,
        "players": [
            {
                "id": player.id,
                "name": player.name,
                "session_digest": player.session_digest,
                "session_expires_at": player.session_expires_at,
                "ready": player.ready,
            }
            for player in room.players.values()
        ],
        "scores": room.scores,
        "committed_operation_ids": room.committed_operation_ids,
        "game": (
            {
                "round_number": room.game.round_number,
                "speaker_id": room.game.speaker_id,
                "prompt": room.game.prompt,
                "statement": room.game.statement,
                "truth_value": room.game.truth_value,
                "votes": room.game.votes,
                "resolved": room.game.resolved,
            }
            if room.game is not None
            else None
        ),
    }


def room_from_state(
    state: dict[str, object],
    *,
    version: int,
    updated_at: int,
    expires_at: int,
) -> Room:
    from app.room_manager import Player, Room, Round

    players = {
        player_data["id"]: Player(
            id=player_data["id"],
            name=player_data["name"],
            session_digest=player_data["session_digest"],
            session_expires_at=player_data["session_expires_at"],
            ready=player_data["ready"],
        )
        for player_data in state["players"]
    }
    game_data = state["game"]
    game = (
        Round(
            round_number=game_data["round_number"],
            speaker_id=game_data["speaker_id"],
            prompt=game_data["prompt"],
            statement=game_data["statement"],
            truth_value=game_data["truth_value"],
            votes=game_data["votes"],
            resolved=game_data["resolved"],
        )
        if game_data is not None
        else None
    )
    return Room(
        id=state["id"],
        name=state["name"],
        players=players,
        scores=state["scores"],
        game=game,
        committed_operation_ids=state.get("committed_operation_ids", []),
        version=version,
        updated_at=updated_at,
        expires_at=expires_at,
    )


def room_to_item(room: Room) -> dict[str, object]:
    state_json = json.dumps(room_to_state(room), separators=(",", ":"), ensure_ascii=True)
    item: dict[str, object] = {
        "room_id": room.id,
        "version": room.version,
        "schema_version": SCHEMA_VERSION,
        "updated_at": room.updated_at,
        "expires_at": room.expires_at,
        "room_state": state_json,
    }
    serialized_size = len(
        json.dumps(item, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    )
    if serialized_size > MAX_DYNAMODB_ITEM_BYTES:
        raise RoomSerializationError("Room state exceeds DynamoDB's 400 KB item limit.")
    return item


def room_from_item(item: dict[str, object]) -> Room:
    if item.get("schema_version") != SCHEMA_VERSION:
        raise RoomRepositoryError("Unsupported room state schema version.")
    try:
        state = json.loads(item["room_state"])
        if not isinstance(state, dict):
            raise TypeError("room_state must be an object")
        return room_from_state(
            state,
            version=int(item["version"]),
            updated_at=int(item["updated_at"]),
            expires_at=int(item["expires_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RoomRepositoryError("Stored room state is invalid.") from exc


class InMemoryRoomRepository:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, object]] = {}

    def load(self, room_id: str) -> Room | None:
        item = self._items.get(room_id)
        return room_from_item(deepcopy(item)) if item is not None else None

    def save(self, room: Room, expected_version: int | None) -> bool:
        current = self._items.get(room.id)
        current_version = current.get("version") if current else None
        if current_version != expected_version:
            return False
        self._items[room.id] = room_to_item(room)
        return True


class DynamoDBRoomRepository:
    def __init__(self, table_name: str, *, region_name: str = "eu-west-1", client=None) -> None:
        if client is None:
            import boto3

            client = boto3.client("dynamodb", region_name=region_name)
        self._client = client
        self._table_name = table_name
        self._serializer = TypeSerializer()

    def load(self, room_id: str) -> Room | None:
        try:
            response = self._client.get_item(
                TableName=self._table_name,
                Key={"room_id": {"S": room_id}},
                ConsistentRead=True,
            )
        except Exception as exc:
            raise RoomRepositoryError("Unable to load room state.") from exc

        attribute_map = response.get("Item")
        if attribute_map is None:
            return None
        try:
            item = {key: _deserialize_attribute(value) for key, value in attribute_map.items()}
            return room_from_item(item)
        except RoomRepositoryError:
            raise
        except Exception as exc:
            raise RoomRepositoryError("Stored room state is invalid.") from exc

    def save(self, room: Room, expected_version: int | None) -> bool:
        item = room_to_item(room)
        encoded_item = {key: self._serializer.serialize(value) for key, value in item.items()}
        parameters: dict[str, object] = {
            "TableName": self._table_name,
            "Item": encoded_item,
        }
        if expected_version is None:
            parameters["ConditionExpression"] = "attribute_not_exists(#room_id)"
            parameters["ExpressionAttributeNames"] = {"#room_id": "room_id"}
        else:
            parameters["ConditionExpression"] = "#version = :expected_version"
            parameters["ExpressionAttributeNames"] = {"#version": "version"}
            parameters["ExpressionAttributeValues"] = {
                ":expected_version": self._serializer.serialize(expected_version)
            }

        try:
            self._client.put_item(**parameters)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            if status_code >= 500:
                raise RoomWriteOutcomeUnknown("DynamoDB write response was ambiguous.") from exc
            raise RoomRepositoryError("Unable to save room state.") from exc
        except (
            ConnectTimeoutError,
            ConnectionClosedError,
            EndpointConnectionError,
            ReadTimeoutError,
        ) as exc:
            raise RoomWriteOutcomeUnknown("DynamoDB write response was ambiguous.") from exc
        except Exception as exc:
            raise RoomRepositoryError("Unable to save room state.") from exc
        return True


def _deserialize_attribute(value: dict[str, object]) -> object:
    from boto3.dynamodb.types import TypeDeserializer

    return TypeDeserializer().deserialize(value)
