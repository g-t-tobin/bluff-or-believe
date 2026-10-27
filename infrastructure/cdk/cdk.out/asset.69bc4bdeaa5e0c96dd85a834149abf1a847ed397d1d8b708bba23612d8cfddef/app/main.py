from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.room_manager import RoomManager

DEFAULT_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

def get_cors_origins() -> list[str]:
    configured = os.getenv("CORS_ORIGINS", "")
    if configured:
        return [origin.strip() for origin in configured.split(",") if origin.strip()]
    return DEFAULT_CORS_ORIGINS

app = FastAPI(
    title="Bluff or Believe: The Big Fib",
    description="Local development API foundation with room and WebSocket session support.",
    version="0.2.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
room_manager = RoomManager()


class RoomCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)


class PlayerJoinRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)


class VoteRequest(BaseModel):
    player_id: str = Field(..., min_length=1, max_length=60)
    belief: bool = True


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/rooms", tags=["rooms"], status_code=201)
async def create_room(payload: RoomCreateRequest) -> dict[str, object]:
    try:
        room = room_manager.create_room(payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return room_manager.room_summary(room.id)


@app.get("/rooms/{room_id}", tags=["rooms"])
async def get_room(room_id: str) -> dict[str, object]:
    room = room_manager.get_room(room_id)
    if room is None:
        raise HTTPException(status_code=404, detail=f"Room '{room_id}' was not found.")

    return room_manager.room_summary(room.id)


@app.post("/rooms/{room_id}/players", tags=["rooms"], status_code=201)
async def join_room(room_id: str, payload: PlayerJoinRequest) -> dict[str, object]:
    try:
        player = room_manager.join_room(room_id, payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "room_id": room_id,
        "player": {"id": player.id, "name": player.name, "ready": player.ready},
    }


@app.post("/rooms/{room_id}/start", tags=["rooms"])
async def start_round(room_id: str) -> dict[str, object]:
    try:
        room_manager.start_round(room_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return room_manager.room_summary(room_id)


@app.post("/rooms/{room_id}/vote", tags=["rooms"])
async def submit_vote(room_id: str, payload: VoteRequest) -> dict[str, object]:
    try:
        room_manager.cast_vote(room_id, payload.player_id, payload.belief)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return room_manager.room_summary(room_id)


@app.websocket("/ws/rooms/{room_id}")
async def room_socket(websocket: WebSocket, room_id: str, player_name: str | None = None) -> None:
    room = room_manager.get_room(room_id)
    if room is None:
        await websocket.close(code=1008, reason="room-not-found")
        return

    await websocket.accept()
    connection_id = room_manager.connect_socket(room_id, websocket)

    if player_name:
        try:
            player = room_manager.join_room(room_id, player_name)
            room_manager.attach_player_to_connection(room_id, connection_id, player.id)
        except ValueError as exc:
            await websocket.send_json({"type": "error", "message": str(exc)})

    await room_manager.broadcast_room_state(room_id)

    try:
        while True:
            payload = await websocket.receive_json()
            event_type = payload.get("type")

            if event_type == "join":
                requested_name = str(
                    payload.get("player_name") or payload.get("name") or ""
                ).strip()
                if not requested_name:
                    await websocket.send_json(
                        {"type": "error", "message": "Player name is required."}
                    )
                    continue

                try:
                    player = room_manager.join_room(room_id, requested_name)
                    room_manager.attach_player_to_connection(room_id, connection_id, player.id)
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
                    continue

            elif event_type == "toggle_ready":
                player_id = room_manager.rooms[room_id].connections.get(connection_id, {}).get(
                    "player_id"
                )
                if player_id is None:
                    await websocket.send_json(
                        {"type": "error", "message": "Join the room before toggling readiness."}
                    )
                    continue

                player = room_manager.rooms[room_id].players.get(player_id)
                if player is None:
                    await websocket.send_json({"type": "error", "message": "Unknown player."})
                    continue

                player.ready = bool(payload.get("ready", False))

            elif event_type == "start_game":
                try:
                    room_manager.start_round(room_id)
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
                    continue

            elif event_type == "vote":
                player_id = room_manager.rooms[room_id].connections.get(connection_id, {}).get(
                    "player_id"
                )
                if player_id is None:
                    await websocket.send_json(
                        {"type": "error", "message": "Join the room before voting."}
                    )
                    continue

                try:
                    room_manager.cast_vote(room_id, player_id, bool(payload.get("belief", False)))
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
                    continue

            elif event_type == "chat":
                player_id = room_manager.rooms[room_id].connections.get(connection_id, {}).get(
                    "player_id"
                )
                message = str(payload.get("message") or "").strip()
                if player_id is None or not message:
                    await websocket.send_json(
                        {"type": "error", "message": "Message requires a joined player and text."}
                    )
                    continue

                player = room_manager.rooms[room_id].players.get(player_id)
                if player is None:
                    await websocket.send_json({"type": "error", "message": "Unknown player."})
                    continue

                await room_manager.broadcast_room_message(room_id, player.name, message)
                continue

            else:
                await websocket.send_json(
                    {"type": "error", "message": f"Unsupported event '{event_type}'."}
                )
                continue

            await room_manager.broadcast_room_state(room_id)

    except WebSocketDisconnect:
        room_manager.disconnect_socket(room_id, connection_id)
        await room_manager.broadcast_room_state(room_id)
