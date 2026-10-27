import os

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from app.room_manager import (
    SESSION_TTL_SECONDS,
    JoinResult,
    Player,
    RoomAuthorizationError,
    RoomManager,
)

DEFAULT_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


def get_cors_origins() -> list[str]:
    configured = os.getenv("CORS_ORIGINS", "")
    if configured:
        return [origin.strip() for origin in configured.split(",") if origin.strip()]
    return DEFAULT_CORS_ORIGINS


def session_cookie_name(room_id: str) -> str:
    return f"__Host-player_session_{room_id}"


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
    model_config = ConfigDict(extra="forbid")

    belief: bool = True


def require_room_player(room_id: str, request: Request) -> Player:
    if room_manager.get_room(room_id) is None:
        raise HTTPException(status_code=404, detail="Room not found.")

    session_token = request.cookies.get(session_cookie_name(room_id))
    player = room_manager.authenticate_session(room_id, session_token)
    if player is None:
        raise HTTPException(status_code=401, detail="A valid room session is required.")
    return player


def require_allowed_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is None or origin not in get_cors_origins():
        raise HTTPException(status_code=403, detail="Request origin is not allowed.")


@app.get("/health", tags=["operations"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/rooms", tags=["rooms"], status_code=201)
async def create_room(payload: RoomCreateRequest) -> dict[str, object]:
    try:
        room = room_manager.create_room(payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Unable to create room.") from exc

    return room_manager.room_summary(room.id)


@app.get("/rooms/{room_id}", tags=["rooms"])
async def get_room(room_id: str, request: Request) -> dict[str, object]:
    player = require_room_player(room_id, request)
    return room_manager.room_summary(room_id, player.id)


@app.post("/rooms/{room_id}/players", tags=["rooms"], status_code=201)
async def join_room(
    room_id: str,
    payload: PlayerJoinRequest,
    request: Request,
    response: Response,
) -> dict[str, object]:
    require_allowed_origin(request)
    session_token = request.cookies.get(session_cookie_name(room_id))
    try:
        result: JoinResult = room_manager.join_room(room_id, payload.name, session_token)
    except RoomAuthorizationError as exc:
        raise HTTPException(status_code=401, detail="A valid room session is required.") from exc
    except ValueError as exc:
        status_code = 404 if room_manager.get_room(room_id) is None else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    if result.session_token is not None:
        response.set_cookie(
            key=session_cookie_name(room_id),
            value=result.session_token,
            max_age=SESSION_TTL_SECONDS,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )

    return {
        "room_id": room_id,
        "player": {
            "id": result.player.id,
            "name": result.player.name,
            "ready": result.player.ready,
        },
    }


@app.post("/rooms/{room_id}/start", tags=["rooms"])
async def start_round(room_id: str, request: Request) -> dict[str, object]:
    require_allowed_origin(request)
    player = require_room_player(room_id, request)
    try:
        room_manager.start_round(room_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return room_manager.room_summary(room_id, player.id)


@app.post("/rooms/{room_id}/vote", tags=["rooms"])
async def submit_vote(
    room_id: str,
    payload: VoteRequest,
    request: Request,
) -> dict[str, object]:
    require_allowed_origin(request)
    player = require_room_player(room_id, request)
    try:
        room_manager.cast_vote(room_id, player.id, payload.belief)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return room_manager.room_summary(room_id, player.id)


@app.websocket("/ws/rooms/{room_id}")
async def room_socket(websocket: WebSocket, room_id: str) -> None:
    room = room_manager.get_room(room_id)
    session_token = websocket.cookies.get(session_cookie_name(room_id))
    player = room_manager.authenticate_session(room_id, session_token)
    origin = websocket.headers.get("origin")

    if (
        room is None
        or player is None
        or origin is None
        or origin not in get_cors_origins()
    ):
        await websocket.close(code=1008)
        return

    session_digest = player.session_digest
    del session_token
    await websocket.accept()
    connection_id = room_manager.connect_socket(room_id, websocket)
    room_manager.attach_player_to_connection(room_id, connection_id, player.id)
    await room_manager.broadcast_room_state(room_id)

    try:
        while True:
            payload = await websocket.receive_json()
            if not isinstance(payload, dict):
                await websocket.send_json({"type": "error", "message": "Invalid event."})
                continue

            current_player = room_manager.get_session_player(
                room_id,
                player.id,
                session_digest,
            )
            if current_player is None:
                await websocket.send_json(
                    {"type": "error", "message": "A valid room session is required."}
                )
                await websocket.close(code=1008)
                break

            event_type = payload.get("type")
            room = room_manager.rooms.get(room_id)
            connection = room.connections.get(connection_id) if room else None
            if (
                connection is None
                or connection.get("player_id") != current_player.id
                or not room_manager.is_connection_authorized(room, connection)
            ):
                await websocket.send_json(
                    {"type": "error", "message": "A valid room session is required."}
                )
                await websocket.close(code=1008)
                break

            if event_type == "toggle_ready":
                ready = payload.get("ready", False)
                if not isinstance(ready, bool):
                    await websocket.send_json({"type": "error", "message": "Invalid event."})
                    continue
                current_player.ready = ready

            elif event_type == "start_game":
                try:
                    room_manager.start_round(room_id)
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
                    continue

            elif event_type == "vote":
                belief = payload.get("belief")
                if not isinstance(belief, bool):
                    await websocket.send_json({"type": "error", "message": "Invalid event."})
                    continue
                try:
                    room_manager.cast_vote(room_id, current_player.id, belief)
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
                    continue

            elif event_type == "chat":
                message = payload.get("message")
                if not isinstance(message, str) or not message.strip():
                    await websocket.send_json({"type": "error", "message": "Invalid event."})
                    continue
                await room_manager.broadcast_room_message(
                    room_id,
                    current_player.name,
                    message.strip(),
                )
                continue

            else:
                await websocket.send_json({"type": "error", "message": "Unsupported event."})
                continue

            await room_manager.broadcast_room_state(room_id)

    except WebSocketDisconnect:
        pass
    finally:
        room_manager.disconnect_socket(room_id, connection_id)
        await room_manager.broadcast_room_state(room_id)
