from __future__ import annotations

import random
from dataclasses import dataclass, field
from uuid import uuid4

from fastapi import WebSocket

PROMPTS = [
    "A risky story from a vacation.",
    "A surprising fact about your childhood.",
    "A ridiculous challenge you once attempted.",
    "A claim that sounds too wild to be true.",
    "A secret skill you once bragged about.",
]


@dataclass(slots=True)
class Player:
    id: str
    name: str
    ready: bool = False


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
    connections: dict[str, dict[str, WebSocket | str | None]] = field(default_factory=dict)
    scores: dict[str, int] = field(default_factory=dict)
    game: Round | None = None


class RoomManager:
    def __init__(self) -> None:
        self.rooms: dict[str, Room] = {}

    def create_room(self, name: str) -> Room:
        room_name = name.strip()
        if not room_name:
            raise ValueError("Room name cannot be empty.")

        room_id = uuid4().hex[:8]
        room = Room(id=room_id, name=room_name)
        self.rooms[room_id] = room
        return room

    def get_room(self, room_id: str) -> Room | None:
        return self.rooms.get(room_id)

    def join_room(self, room_id: str, player_name: str) -> Player:
        room = self.rooms.get(room_id)
        if room is None:
            raise ValueError(f"Unknown room '{room_id}'.")

        candidate_name = player_name.strip()
        if not candidate_name:
            raise ValueError("Player name cannot be empty.")

        existing_player = next(
            (
                existing
                for existing in room.players.values()
                if existing.name.lower() == candidate_name.lower()
            ),
            None,
        )
        if existing_player is not None:
            return existing_player

        player = Player(id=f"player-{uuid4().hex[:8]}", name=candidate_name)
        room.players[player.id] = player
        room.scores.setdefault(player.id, 0)
        return player

    def connect_socket(self, room_id: str, websocket: WebSocket) -> str:
        room = self.rooms.get(room_id)
        if room is None:
            raise ValueError(f"Unknown room '{room_id}'.")

        connection_id = uuid4().hex
        room.connections[connection_id] = {"websocket": websocket, "player_id": None}
        return connection_id

    def attach_player_to_connection(self, room_id: str, connection_id: str, player_id: str) -> None:
        room = self.rooms.get(room_id)
        if room is None:
            raise ValueError(f"Unknown room '{room_id}'.")

        connection = room.connections.get(connection_id)
        if connection is None:
            raise ValueError("Unknown socket connection.")

        for active_connection_id, active_connection in list(room.connections.items()):
            if active_connection_id != connection_id and active_connection.get("player_id") == player_id:
                active_connection["player_id"] = None
                room.connections.pop(active_connection_id, None)

        connection["player_id"] = player_id

    def disconnect_socket(self, room_id: str, connection_id: str) -> None:
        room = self.rooms.get(room_id)
        if room is None:
            return

        room.connections.pop(connection_id, None)

    def start_round(self, room_id: str) -> Round:
        room = self.rooms.get(room_id)
        if room is None:
            raise ValueError(f"Unknown room '{room_id}'.")

        if len(room.players) < 2:
            raise ValueError("At least two players are required to start a round.")

        if room.game is not None and not room.game.resolved:
            raise ValueError("The current round has not been resolved yet.")

        next_round_number = 1 if room.game is None else room.game.round_number + 1
        players = list(room.players.values())
        speaker = players[(next_round_number - 1) % len(players)]
        truth_value = random.choice([True, False])
        prompt = random.choice(PROMPTS)
        statement = (
            f"{speaker.name} says: \"I have actually done this before.\""
            if truth_value
            else f"{speaker.name} says: \"I have never done this before.\""
        )

        room.game = Round(
            round_number=next_round_number,
            speaker_id=speaker.id,
            prompt=prompt,
            statement=statement,
            truth_value=truth_value,
        )
        return room.game

    def cast_vote(self, room_id: str, player_id: str, value: bool) -> Round:
        room = self.rooms.get(room_id)
        if room is None:
            raise ValueError(f"Unknown room '{room_id}'.")

        if room.game is None:
            raise ValueError("There is no active round to vote on.")

        if player_id not in room.players:
            raise ValueError(f"Unknown player '{player_id}'.")

        room.game.cast_vote(player_id, value)

        if len(room.game.votes) == len(room.players) - 1:
            self.resolve_round(room_id)

        return room.game

    def resolve_round(self, room_id: str) -> dict[str, int]:
        room = self.rooms.get(room_id)
        if room is None:
            raise ValueError(f"Unknown room '{room_id}'.")

        if room.game is None:
            raise ValueError("There is no active round to resolve.")

        if room.game.resolved:
            return room.scores

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

    def room_summary(self, room_id: str) -> dict[str, object]:
        room = self.rooms.get(room_id)
        if room is None:
            raise ValueError(f"Unknown room '{room_id}'.")

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
            payload["game"] = {
                "round_number": room.game.round_number,
                "speaker_id": room.game.speaker_id,
                "prompt": room.game.prompt,
                "statement": room.game.statement,
                "truth_value": room.game.truth_value,
                "votes": {player_id: value for player_id, value in room.game.votes.items()},
                "resolved": room.game.resolved,
            }

        return payload

    async def broadcast_room_state(self, room_id: str) -> None:
        room = self.rooms.get(room_id)
        if room is None:
            return

        payload = {
            "type": "room_state",
            "room": self.room_summary(room_id),
        }

        for connection in room.connections.values():
            websocket = connection["websocket"]
            if isinstance(websocket, WebSocket):
                await websocket.send_json(payload)

    async def broadcast_room_message(self, room_id: str, author: str, message: str) -> None:
        room = self.rooms.get(room_id)
        if room is None:
            return

        payload = {
            "type": "chat",
            "author": author,
            "message": message,
        }

        for connection in room.connections.values():
            websocket = connection["websocket"]
            if isinstance(websocket, WebSocket):
                await websocket.send_json(payload)
