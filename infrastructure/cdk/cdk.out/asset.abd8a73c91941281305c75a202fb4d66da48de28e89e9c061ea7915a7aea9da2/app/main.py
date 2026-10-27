import asyncio
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
    RoomOutcomeUncertainError,
    RoomPersistenceError,
)
from app.room_repository import DynamoDBRoomRepository, InMemoryRoomRepository

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


def create_room_manager() -> RoomManager:
    if os.getenv("APP_ENV", "local").lower() == "production":
        table_name = os.getenv("ROOMS_TABLE_NAME")
        if not table_name:
            raise RuntimeError("ROOMS_TABLE_NAME is required in production.")
        return RoomManager(
            DynamoDBRoomRepository(
                table_name,
                region_name=os.getenv("AWS_REGION", "eu-west-1"),
            )
        )
    return RoomManager()


room_manager = create_room_manager()


async def manager_call(method, *args):
    if isinstance(room_manager.repository, InMemoryRoomRepository):
        return method(*args)
    return await asyncio.to_thread(method, *args)


class RoomCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)


class PlayerJoinRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)


class VoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    belief: bool = True


async def require_room_player(room_id: str, request: Request) -> Player:
    try:
        if await manager_call(room_manager.get_room, room_id) is None:
            raise HTTPException(status_code=404, detail="Room not found.")

        session_token = request.cookies.get(session_cookie_name(room_id))
        player = await manager_call(room_manager.authenticate_session, room_id, session_token)
    except RoomPersistenceError as exc:
        raise HTTPException(
            status_code=503,
            detail="Room storage is temporarily unavailable.",
        ) from exc
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
        room = await manager_call(room_manager.create_room, payload.name)
        return room_manager.cached_room_summary(room.id)
    except RoomOutcomeUncertainError as exc:
        raise HTTPException(
            status_code=503,
            detail="Room creation outcome is uncertain. Refresh before retrying.",
        ) from exc
    except RoomPersistenceError as exc:
        raise HTTPException(
            status_code=503,
            detail="Room could not be saved. Please try again.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Unable to create room.") from exc


@app.get("/rooms/{room_id}", tags=["rooms"])
async def get_room(room_id: str, request: Request, response: Response) -> dict[str, object]:
    cookie_name = session_cookie_name(room_id)
    try:
        room = await manager_call(room_manager.get_room, room_id)
    except RoomPersistenceError as exc:
        raise HTTPException(
            status_code=503,
            detail="Room storage is temporarily unavailable.",
        ) from exc
    if room is None:
        response.delete_cookie(
            key=cookie_name,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        response.status_code = 404
        return {"detail": "Room not found."}

    try:
        player = await manager_call(
            room_manager.authenticate_session,
            room_id,
            request.cookies.get(cookie_name),
        )
    except RoomPersistenceError as exc:
        raise HTTPException(
            status_code=503,
            detail="Room storage is temporarily unavailable.",
        ) from exc
    if player is None:
        response.delete_cookie(
            key=cookie_name,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        response.status_code = 401
        return {"detail": "A valid room session is required."}

    try:
        return await manager_call(room_manager.room_summary, room_id, player.id)
    except RoomPersistenceError as exc:
        raise HTTPException(
            status_code=503,
            detail="Room storage is temporarily unavailable.",
        ) from exc
    except ValueError:
        response.delete_cookie(
            key=cookie_name,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        response.status_code = 404
        return {"detail": "Room not found."}


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
        result: JoinResult = await manager_call(
            room_manager.join_room,
            room_id,
            payload.name,
            session_token,
        )
    except RoomOutcomeUncertainError as exc:
        raise HTTPException(
            status_code=503,
            detail="Join outcome is uncertain. Refresh the room before retrying.",
        ) from exc
    except RoomPersistenceError as exc:
        raise HTTPException(
            status_code=503,
            detail="Player could not be saved. Please try again.",
        ) from exc
    except RoomAuthorizationError:
        response.delete_cookie(
            key=session_cookie_name(room_id),
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        response.status_code = 401
        return {"detail": "A valid room session is required."}
    except ValueError as exc:
        try:
            status_code = 404 if await manager_call(room_manager.get_room, room_id) is None else 409
        except RoomPersistenceError as storage_exc:
            raise HTTPException(
                status_code=503,
                detail="Room storage is temporarily unavailable.",
            ) from storage_exc
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
    player = await require_room_player(room_id, request)
    try:
        await manager_call(room_manager.start_round, room_id)
        return room_manager.cached_room_summary(room_id, player.id)
    except RoomOutcomeUncertainError as exc:
        raise HTTPException(
            status_code=503,
            detail="Action outcome is uncertain. Refresh the room before retrying.",
        ) from exc
    except RoomPersistenceError as exc:
        raise HTTPException(
            status_code=503,
            detail="Game action could not be saved. Please try again.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/rooms/{room_id}/vote", tags=["rooms"])
async def submit_vote(
    room_id: str,
    payload: VoteRequest,
    request: Request,
) -> dict[str, object]:
    require_allowed_origin(request)
    player = await require_room_player(room_id, request)
    try:
        await manager_call(room_manager.cast_vote, room_id, player.id, payload.belief)
        return room_manager.cached_room_summary(room_id, player.id)
    except RoomOutcomeUncertainError as exc:
        raise HTTPException(
            status_code=503,
            detail="Action outcome is uncertain. Refresh the room before retrying.",
        ) from exc
    except RoomPersistenceError as exc:
        raise HTTPException(
            status_code=503,
            detail="Game action could not be saved. Please try again.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.websocket("/ws/rooms/{room_id}")
async def room_socket(websocket: WebSocket, room_id: str) -> None:
    session_token = websocket.cookies.get(session_cookie_name(room_id))
    origin = websocket.headers.get("origin")

    if origin is None or origin not in get_cors_origins():
        await websocket.close(code=1008)
        return

    try:
        player = await manager_call(room_manager.reconnect_room, room_id, session_token)
    except RoomOutcomeUncertainError:
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "error",
                "code": "outcome_uncertain",
                "message": "Reconnect to refresh the room before retrying.",
            }
        )
        await websocket.close(code=1013)
        return
    except RoomPersistenceError:
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "error",
                "code": "storage_unavailable",
                "message": "Room storage is unavailable. Please reconnect later.",
            }
        )
        await websocket.close(code=1013)
        return
    except ValueError:
        player = None
    if player is None:
        await websocket.close(code=1008)
        return

    session_digest = player.session_digest
    await websocket.accept()
    connection_id = await manager_call(room_manager.connect_socket, room_id, websocket)
    await manager_call(
        room_manager.attach_player_to_connection,
        room_id,
        connection_id,
        player.id,
    )
    await room_manager.broadcast_room_state(room_id)

    async def report_uncertain_action() -> None:
        try:
            latest_room = await manager_call(
                room_manager.room_summary,
                room_id,
                player.id,
            )
        except (RoomPersistenceError, ValueError):
            latest_room = None
        if latest_room is not None:
            await websocket.send_json({"type": "room_state", "room": latest_room})
        await websocket.send_json(
            {
                "type": "error",
                "code": "outcome_uncertain",
                "message": (
                    "Action outcome is uncertain. The room was refreshed; "
                    "do not retry unless it is still needed."
                ),
            }
        )
        await websocket.close(code=1013)

    try:
        while True:
            payload = await websocket.receive_json()
            if not isinstance(payload, dict):
                await websocket.send_json({"type": "error", "message": "Invalid event."})
                continue

            try:
                current_player = await manager_call(
                    room_manager.get_session_player,
                    room_id,
                    player.id,
                    session_digest,
                )
            except RoomPersistenceError:
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": "Room storage is unavailable. Please reconnect later.",
                    }
                )
                await websocket.close(code=1013)
                break
            if current_player is None:
                await websocket.send_json(
                    {"type": "error", "message": "A valid room session is required."}
                )
                await websocket.close(code=1008)
                break

            event_type = payload.get("type")
            room = room_manager.get_cached_room(room_id)
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
                try:
                    await manager_call(room_manager.set_ready, room_id, current_player.id, ready)
                except RoomOutcomeUncertainError:
                    await report_uncertain_action()
                    break
                except RoomPersistenceError:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": "Game action could not be saved. Please try again.",
                        }
                    )
                    continue
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
                    continue

            elif event_type == "start_game":
                try:
                    await manager_call(room_manager.start_round, room_id)
                except RoomOutcomeUncertainError:
                    await report_uncertain_action()
                    break
                except RoomPersistenceError:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": "Game action could not be saved. Please try again.",
                        }
                    )
                    continue
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc)})
                    continue

            elif event_type == "vote":
                belief = payload.get("belief")
                if not isinstance(belief, bool):
                    await websocket.send_json({"type": "error", "message": "Invalid event."})
                    continue
                try:
                    await manager_call(
                        room_manager.cast_vote,
                        room_id,
                        current_player.id,
                        belief,
                    )
                except RoomOutcomeUncertainError:
                    await report_uncertain_action()
                    break
                except RoomPersistenceError:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": "Game action could not be saved. Please try again.",
                        }
                    )
                    continue
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
