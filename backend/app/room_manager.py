from __future__ import annotations

import base64
import random
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from hashlib import sha256
from hmac import compare_digest
from hmac import new as hmac_new
from uuid import uuid4

from fastapi import WebSocket

from app.room_repository import (
    MAX_OPERATION_RECEIPTS,
    InMemoryRoomRepository,
    RoomRepository,
    RoomRepositoryError,
    RoomWriteOutcomeUnknown,
    room_from_item,
    room_to_item,
)

SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
ROOM_TTL_SECONDS = 24 * 60 * 60
MAX_MUTATION_ATTEMPTS = 5
_LOCAL_SESSION_SECRET = secrets.token_bytes(32)
_JOIN_KEY_PATTERN = re.compile(r"^j1\.([0-9a-f]{64})\.([0-9a-f]{64})$")

PROMPTS = [
    "A risky story from a vacation.",
    "A surprising fact about your childhood.",
    "A ridiculous challenge you once attempted.",
    "A claim that sounds too wild to be true.",
    "A secret skill you once bragged about.",
]


def validate_join_idempotency_key(room_id: str, player_name: str, key: str) -> bool:
    match = _JOIN_KEY_PATTERN.fullmatch(key)
    if match is None:
        return False
    nonce, supplied_digest = match.groups()
    request_binding = f"join-v1\0{room_id}\0{player_name.strip()}\0{nonce}"
    expected_digest = sha256(request_binding.encode("utf-8")).hexdigest()
    return compare_digest(supplied_digest, expected_digest)


@dataclass(slots=True)
class Player:
    id: str
    name: str
    session_digest: str
    session_expires_at: float
    ready: bool = False
    join_idempotency_digest: str | None = None


@dataclass(slots=True)
class JoinResult:
    player: Player
    session_token: str | None


@dataclass(slots=True)
class Round:
    round_number: int
    speaker_id: str
    prompt: str
    statement: str
    truth_value: bool
    votes: dict[str, bool] = field(default_factory=dict)
    resolved: bool = False

    def cast_vote(self, player_id: str, value: bool) -> None:
        if player_id == self.speaker_id:
            raise ValueError("The speaker cannot vote on their own statement.")
        if player_id in self.votes:
            raise ValueError(f"Player '{player_id}' has already voted.")
        self.votes[player_id] = value


@dataclass(slots=True)
class Room:
    id: str
    name: str
    players: dict[str, Player] = field(default_factory=dict)
    connections: dict[str, dict[str, object]] = field(default_factory=dict)
    scores: dict[str, int] = field(default_factory=dict)
    game: Round | None = None
    version: int = 0
    updated_at: int = 0
    expires_at: int = 0
    committed_operation_ids: list[str] = field(default_factory=list)


class RoomManager:
    def __init__(
        self,
        repository: RoomRepository | None = None,
        *,
        session_secret: bytes | None = None,
    ) -> None:
        self.repository = repository or InMemoryRoomRepository()
        self._session_secret = session_secret or _LOCAL_SESSION_SECRET
        if len(self._session_secret) < 32:
            raise ValueError("Room session secret must contain at least 32 bytes.")
        self._rooms: dict[str, Room] = {}
        self._lock = threading.RLock()

    def create_room(self, name: str, operation_id: str | None = None) -> Room:
        room_name = name.strip()
        if not room_name:
            raise ValueError("Room name cannot be empty.")

        operation_id = operation_id or uuid4().hex
        room_id = operation_id
        with self._lock:
            try:
                existing = self.repository.load(room_id)
            except Exception as exc:
                raise RoomPersistenceError("Unable to load room state.") from exc
            if existing is not None:
                if operation_id in existing.committed_operation_ids and existing.name == room_name:
                    if existing.expires_at <= time.time():
                        raise ValueError("This room creation key belongs to an expired room.")
                    self._rooms[room_id] = existing
                    return existing
                raise ValueError("This room creation key has already been used.")

        now = time.time()
        room = Room(
            id=room_id,
            name=room_name,
            version=1,
            updated_at=int(now),
            expires_at=int(now + ROOM_TTL_SECONDS),
            committed_operation_ids=[operation_id],
        )
        last_error: Exception | None = None
        for _ in range(MAX_MUTATION_ATTEMPTS):
            try:
                persisted = self.repository.load(room.id)
            except Exception as read_exc:
                raise RoomOutcomeUncertainError(
                    f"Room creation outcome is uncertain for room '{room.id}'."
                ) from read_exc
            if persisted is not None:
                if operation_id in persisted.committed_operation_ids:
                    if persisted.expires_at <= time.time():
                        raise RoomOutcomeUncertainError(
                            f"Room '{room.id}' was created but has expired."
                        )
                    room = persisted
                    break
                raise ValueError("This room creation key has already been used.")
            try:
                if self.repository.save(room, expected_version=None):
                    break
                last_error = RoomWriteConflict(
                    "Room creation collided with an existing room identifier."
                )
            except RoomWriteOutcomeUnknown as exc:
                last_error = exc
                continue
            except RoomRepositoryError as exc:
                if last_error is not None:
                    last_error = exc
                    continue
                raise RoomPersistenceError("Unable to save the new room.") from exc
            except Exception as exc:
                if last_error is not None:
                    last_error = exc
                    continue
                raise RoomPersistenceError("Unable to save the new room.") from exc
        else:
            raise RoomOutcomeUncertainError(
                f"Room creation outcome is uncertain for room '{room.id}'."
            ) from last_error

        with self._lock:
            self._rooms[room.id] = room
        return room

    def get_room(self, room_id: str) -> Room | None:
        try:
            room = self.repository.load(room_id)
        except RoomRepositoryError as exc:
            if isinstance(exc, RoomPersistenceError):
                raise
            raise RoomPersistenceError("Unable to load room state.") from exc
        except Exception as exc:
            raise RoomPersistenceError("Unable to load room state.") from exc
        if room is None or room.expires_at <= time.time():
            with self._lock:
                self._rooms.pop(room_id, None)
            return None
        with self._lock:
            cached_room = self._rooms.get(room_id)
            if cached_room is not None:
                room.connections = cached_room.connections
            self._rooms[room_id] = room
        return room

    def join_room(
        self,
        room_id: str,
        player_name: str,
        session_token: str | None = None,
        idempotency_key: str | None = None,
    ) -> JoinResult:
        room = self.get_room(room_id)
        if room is None:
            raise ValueError(f"Unknown room '{room_id}'.")

        candidate_name = player_name.strip()
        if not candidate_name:
            raise ValueError("Player name cannot be empty.")

        key = idempotency_key or secrets.token_urlsafe(32)
        key_digest = sha256(key.encode("utf-8")).hexdigest()
        player_id = self._join_player_id(room_id, key_digest)
        token = self._join_session_token(room_id, key_digest)
        matching_player = next(
            (
                player
                for player in room.players.values()
                if player.join_idempotency_digest == key_digest
            ),
            None,
        )

        authenticated_player = (
            self.authenticate_session(room_id, session_token) if session_token else None
        )
        if session_token is not None and authenticated_player is None:
            raise RoomAuthorizationError("A valid room session is required.")

        if matching_player is not None:
            if matching_player.name != candidate_name:
                raise ValueError("Join idempotency key was already used with different details.")
            if not compare_digest(
                matching_player.session_digest,
                self._digest_session(token),
            ):
                raise RoomPersistenceError(
                    "Room session recovery secret does not match the stored session."
                )
            if authenticated_player is not None and authenticated_player.id != matching_player.id:
                raise RoomAuthorizationError("A valid room session is required.")
            if matching_player.session_expires_at <= time.time():
                def refresh_session(candidate: Room) -> Player:
                    player = candidate.players.get(matching_player.id)
                    if (
                        player is None
                        or player.name != candidate_name
                        or player.join_idempotency_digest != key_digest
                    ):
                        raise RoomAuthorizationError("A valid join idempotency key is required.")
                    player.session_digest = self._digest_session(token)
                    player.session_expires_at = time.time() + SESSION_TTL_SECONDS
                    return player

                matching_player = self._mutate(
                    room_id,
                    refresh_session,
                    operation_id=f"join-refresh-{key_digest}-{uuid4().hex}",
                )
            return JoinResult(player=matching_player, session_token=token)

        if authenticated_player is not None:
            if authenticated_player.name.casefold() != candidate_name.casefold():
                raise RoomAuthorizationError("A valid room session is required.")
            return JoinResult(player=authenticated_player, session_token=None)

        player = Player(
            id=player_id,
            name=candidate_name,
            session_digest=self._digest_session(token),
            session_expires_at=time.time() + SESSION_TTL_SECONDS,
            join_idempotency_digest=key_digest,
        )

        def add_player(candidate: Room) -> Player:
            existing_key_player = next(
                (
                    existing
                    for existing in candidate.players.values()
                    if existing.join_idempotency_digest == key_digest
                ),
                None,
            )
            if existing_key_player is not None:
                if existing_key_player.name != candidate_name:
                    raise ValueError(
                        "Join idempotency key was already used with different details."
                    )
                return existing_key_player
            if any(
                existing.name.casefold() == candidate_name.casefold()
                for existing in candidate.players.values()
            ):
                raise ValueError("That player name is already in use.")
            candidate.players[player.id] = player
            candidate.scores.setdefault(player.id, 0)
            return player

        saved_player = self._mutate(room_id, add_player, operation_id=key_digest)
        return JoinResult(player=saved_player, session_token=token)

    def _join_session_token(self, room_id: str, key_digest: str) -> str:
        payload = f"room-session-v1\0{room_id}\0{key_digest}".encode()
        token_digest = hmac_new(self._session_secret, payload, "sha256").digest()
        return base64.urlsafe_b64encode(token_digest).rstrip(b"=").decode("ascii")

    def _join_player_id(self, room_id: str, key_digest: str) -> str:
        payload = f"player-id-v1\0{room_id}\0{key_digest}".encode()
        identifier = hmac_new(self._session_secret, payload, "sha256").hexdigest()
        return f"player-{identifier[:32]}"

    def authenticate_session(self, room_id: str, session_token: str | None) -> Player | None:
        room = self.get_room(room_id)
        if room is None or not session_token:
            return None

        candidate_digest = self._digest_session(session_token)
        for player in room.players.values():
            if player.session_expires_at > time.time() and compare_digest(
                player.session_digest, candidate_digest
            ):
                return player
        return None

    def get_session_player(
        self,
        room_id: str,
        player_id: str,
        session_digest: str,
    ) -> Player | None:
        room = self.get_room(room_id)
        player = room.players.get(player_id) if room else None
        if (
            player is None
            or player.session_expires_at <= time.time()
            or not compare_digest(player.session_digest, session_digest)
        ):
            return None
        return player

    def reconnect_room(self, room_id: str, session_token: str | None) -> Player | None:
        if not session_token:
            return None
        digest = self._digest_session(session_token)
        authenticated = self.authenticate_session(room_id, session_token)
        if authenticated is None:
            return None

        def refresh(candidate: Room) -> Player:
            player = candidate.players.get(authenticated.id)
            if (
                player is None
                or player.session_expires_at <= time.time()
                or not compare_digest(player.session_digest, digest)
            ):
                raise RoomAuthorizationError("A valid room session is required.")
            return player

        return self._mutate(room_id, refresh)

    @staticmethod
    def _digest_session(session_token: str) -> str:
        return sha256(session_token.encode("utf-8")).hexdigest()

    def connect_socket(self, room_id: str, websocket: WebSocket) -> str:
        room = self._rooms.get(room_id)
        if room is None:
            raise ValueError(f"Unknown room '{room_id}'.")

        connection_id = uuid4().hex
        room.connections[connection_id] = {"websocket": websocket, "player_id": None}
        return connection_id

    def attach_player_to_connection(self, room_id: str, connection_id: str, player_id: str) -> None:
        room = self._rooms.get(room_id)
        if room is None:
            raise ValueError(f"Unknown room '{room_id}'.")

        connection = room.connections.get(connection_id)
        if connection is None:
            raise ValueError("Unknown socket connection.")

        player = room.players.get(player_id)
        if player is None or player.session_expires_at <= time.time():
            raise RoomAuthorizationError("A valid room session is required.")

        for active_connection_id, active_connection in list(room.connections.items()):
            if (
                active_connection_id != connection_id
                and active_connection.get("player_id") == player_id
            ):
                active_connection["player_id"] = None
                room.connections.pop(active_connection_id, None)

        connection["player_id"] = player_id
        connection["session_digest"] = player.session_digest

    def disconnect_socket(self, room_id: str, connection_id: str) -> None:
        room = self._rooms.get(room_id)
        if room is None:
            return

        room.connections.pop(connection_id, None)

    def get_cached_room(self, room_id: str) -> Room | None:
        return self._rooms.get(room_id)

    def set_ready(self, room_id: str, player_id: str, ready: bool) -> Player:
        def update(candidate: Room) -> Player:
            player = candidate.players.get(player_id)
            if player is None or player.session_expires_at <= time.time():
                raise RoomAuthorizationError("A valid room session is required.")
            player.ready = ready
            return player

        return self._mutate(room_id, update)

    def start_round(self, room_id: str) -> Round:
        def start(candidate: Room) -> Round:
            if len(candidate.players) < 2:
                raise ValueError("At least two players are required to start a round.")
            if candidate.game is not None and not candidate.game.resolved:
                raise ValueError("The current round has not been resolved yet.")

            next_round_number = 1 if candidate.game is None else candidate.game.round_number + 1
            players = list(candidate.players.values())
            speaker = players[(next_round_number - 1) % len(players)]
            truth_value = random.choice([True, False])
            prompt = random.choice(PROMPTS)
            statement = (
                f'{speaker.name} says: "I have actually done this before."'
                if truth_value
                else f'{speaker.name} says: "I have never done this before."'
            )
            candidate.game = Round(
                round_number=next_round_number,
                speaker_id=speaker.id,
                prompt=prompt,
                statement=statement,
                truth_value=truth_value,
            )
            return candidate.game

        return self._mutate(room_id, start)

    def cast_vote(self, room_id: str, player_id: str, value: bool) -> Round:
        def cast(candidate: Room) -> Round:
            if candidate.game is None:
                raise ValueError("There is no active round to vote on.")
            if player_id not in candidate.players:
                raise ValueError(f"Unknown player '{player_id}'.")
            candidate.game.cast_vote(player_id, value)
            if len(candidate.game.votes) == len(candidate.players) - 1:
                self._resolve(candidate)
            return candidate.game

        return self._mutate(room_id, cast)

    def resolve_round(self, room_id: str) -> dict[str, int]:
        return self._mutate(room_id, self._resolve)

    @staticmethod
    def _resolve(room: Room) -> dict[str, int]:
        if room.game is None:
            raise ValueError("There is no active round to resolve.")
        if room.game.resolved:
            raise ValueError("The current round has already been resolved.")
        expected_players = {
            player_id for player_id in room.players if player_id != room.game.speaker_id
        }
        if set(room.game.votes) != expected_players:
            raise ValueError("All players except the speaker must vote before resolving the round.")

        correct_voters = [
            player_id
            for player_id, vote in room.game.votes.items()
            if vote == room.game.truth_value
        ]

        room.scores.setdefault(room.game.speaker_id, 0)
        room.scores[room.game.speaker_id] += len(correct_voters) * 2
        for player_id in correct_voters:
            room.scores.setdefault(player_id, 0)
            room.scores[player_id] += 2

        room.game.resolved = True
        return dict(room.scores)

    def _mutate(self, room_id: str, operation, *, operation_id: str | None = None):
        operation_id = operation_id or uuid4().hex
        pending_candidate: Room | None = None
        pending_result = None
        pending_expected_version: int | None = None
        last_ambiguous_error: Exception | None = None
        with self._lock:
            for _ in range(MAX_MUTATION_ATTEMPTS):
                try:
                    current = self.repository.load(room_id)
                except RoomRepositoryError as exc:
                    if pending_candidate is not None:
                        raise RoomOutcomeUncertainError(
                            "Action outcome is uncertain; refresh the room before retrying."
                        ) from exc
                    if isinstance(exc, RoomPersistenceError):
                        raise
                    raise RoomPersistenceError("Unable to load room state.") from exc
                except Exception as exc:
                    if pending_candidate is not None:
                        raise RoomOutcomeUncertainError(
                            "Action outcome is uncertain; refresh the room before retrying."
                        ) from exc
                    raise RoomPersistenceError("Unable to load room state.") from exc
                if current is None:
                    if pending_candidate is not None:
                        raise RoomOutcomeUncertainError(
                            "Action outcome is uncertain; refresh the room before retrying."
                        ) from last_ambiguous_error
                    raise ValueError(f"Unknown room '{room_id}'.")
                if current.expires_at <= time.time():
                    self._rooms.pop(room_id, None)
                    if pending_candidate is not None:
                        raise RoomOutcomeUncertainError(
                            "Action outcome is uncertain; refresh the room before retrying."
                        ) from last_ambiguous_error
                    raise ValueError("Room has expired.")

                cached = self._rooms.get(room_id)
                if pending_candidate is not None:
                    if operation_id in current.committed_operation_ids:
                        if cached is not None:
                            current.connections = cached.connections
                        self._rooms[room_id] = current
                        return pending_result
                    if current.version == pending_expected_version:
                        try:
                            saved = self.repository.save(
                                pending_candidate,
                                expected_version=pending_expected_version,
                            )
                        except RoomWriteOutcomeUnknown as exc:
                            last_ambiguous_error = exc
                            continue
                        except RoomRepositoryError as exc:
                            last_ambiguous_error = exc
                            continue
                        except Exception as exc:
                            raise RoomPersistenceError("Unable to save room state.") from exc
                        if saved:
                            self._rooms[room_id] = pending_candidate
                            return pending_result
                        # The original candidate can no longer commit after this CAS
                        # conflict. Reload on the next pass and check its receipt first.
                        continue
                    if current.version < (pending_expected_version or 0):
                        raise RoomOutcomeUncertainError(
                            "Action outcome is uncertain; refresh the room before retrying."
                        ) from last_ambiguous_error
                    if current.version - (pending_expected_version or 0) > MAX_OPERATION_RECEIPTS:
                        raise RoomOutcomeUncertainError(
                            "Action outcome is uncertain; refresh the room before retrying."
                        ) from last_ambiguous_error
                    pending_candidate = None
                    pending_result = None
                    pending_expected_version = None
                    last_ambiguous_error = None

                if cached is not None:
                    current.connections = cached.connections
                try:
                    candidate = room_from_item(room_to_item(current))
                except RoomRepositoryError as exc:
                    raise RoomPersistenceError("Unable to serialize room state.") from exc
                candidate.connections = current.connections
                result = operation(candidate)
                now = time.time()
                candidate.version = current.version + 1
                candidate.updated_at = int(now)
                candidate.expires_at = int(now + ROOM_TTL_SECONDS)
                candidate.committed_operation_ids = (
                    current.committed_operation_ids + [operation_id]
                )[-MAX_OPERATION_RECEIPTS:]
                try:
                    saved = self.repository.save(candidate, expected_version=current.version)
                except RoomWriteOutcomeUnknown as exc:
                    pending_candidate = candidate
                    pending_result = result
                    pending_expected_version = current.version
                    last_ambiguous_error = exc
                    continue
                except RoomRepositoryError as exc:
                    if isinstance(exc, RoomPersistenceError):
                        raise
                    raise RoomPersistenceError("Unable to save room state.") from exc
                except Exception as exc:
                    raise RoomPersistenceError("Unable to save room state.") from exc
                if saved:
                    self._rooms[room_id] = candidate
                    return result
            if pending_candidate is not None:
                raise RoomOutcomeUncertainError(
                    "Action outcome is uncertain; refresh the room before retrying."
                ) from last_ambiguous_error
            raise RoomWriteConflict("Room changed concurrently; retry the action.")

    def room_summary(
        self,
        room_id: str,
        viewer_player_id: str | None = None,
    ) -> dict[str, object]:
        room = self.get_room(room_id)
        if room is None:
            raise ValueError(f"Unknown room '{room_id}'.")

        return self._room_summary(room, viewer_player_id)

    def cached_room_summary(
        self,
        room_id: str,
        viewer_player_id: str | None = None,
    ) -> dict[str, object]:
        room = self._rooms.get(room_id)
        if room is None or room.expires_at <= time.time():
            raise ValueError(f"Unknown room '{room_id}'.")
        return self._room_summary(room, viewer_player_id)

    @staticmethod
    def _room_summary(room: Room, viewer_player_id: str | None) -> dict[str, object]:
        payload: dict[str, object] = {
            "room_id": room.id,
            "name": room.name,
            "players": [
                {"id": player.id, "name": player.name, "ready": player.ready}
                for player in room.players.values()
            ],
            "scores": {player_id: score for player_id, score in room.scores.items()},
        }

        if room.game is not None:
            game_payload: dict[str, object] = {
                "round_number": room.game.round_number,
                "speaker_id": room.game.speaker_id,
                "prompt": room.game.prompt,
                "statement": room.game.statement,
                "resolved": room.game.resolved,
            }
            if room.game.resolved:
                game_payload["truth_value"] = room.game.truth_value
                game_payload["votes"] = dict(room.game.votes)
            elif viewer_player_id is not None:
                game_payload["has_voted"] = viewer_player_id in room.game.votes
            payload["game"] = game_payload

        return payload

    async def broadcast_room_state(self, room_id: str) -> None:
        room = self._rooms.get(room_id)
        if room is None:
            return

        for connection_id, connection in list(room.connections.items()):
            if not self.is_connection_authorized(room, connection):
                room.connections.pop(connection_id, None)
                continue
            websocket = connection["websocket"]
            if isinstance(websocket, WebSocket):
                player_id = connection.get("player_id")
                await websocket.send_json(
                    {
                        "type": "room_state",
                        "room": self._room_summary(
                            room,
                            player_id if isinstance(player_id, str) else None,
                        ),
                    }
                )

    async def broadcast_room_message(self, room_id: str, author: str, message: str) -> None:
        room = self._rooms.get(room_id)
        if room is None:
            return

        payload = {
            "type": "chat",
            "author": author,
            "message": message,
        }

        for connection_id, connection in list(room.connections.items()):
            if not self.is_connection_authorized(room, connection):
                room.connections.pop(connection_id, None)
                continue
            websocket = connection["websocket"]
            if isinstance(websocket, WebSocket):
                await websocket.send_json(payload)

    @staticmethod
    def is_connection_authorized(room: Room, connection: dict[str, object]) -> bool:
        player_id = connection.get("player_id")
        player = room.players.get(player_id) if isinstance(player_id, str) else None
        session_digest = connection.get("session_digest")
        return bool(
            player
            and player.session_expires_at > time.time()
            and isinstance(session_digest, str)
            and compare_digest(player.session_digest, session_digest)
        )


class RoomAuthorizationError(ValueError):
    """Raised when a room session is missing, invalid, expired, or mismatched."""


class RoomPersistenceError(RoomRepositoryError):
    """Raised when durable room state cannot be accessed or committed."""


class RoomWriteConflict(RoomPersistenceError):
    """Raised when a room remains contended after bounded optimistic retries."""


class RoomOutcomeUncertainError(RoomPersistenceError):
    """Raised when a write's commit status cannot be established safely."""
