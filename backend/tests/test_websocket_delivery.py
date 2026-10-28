import asyncio
from hashlib import sha256
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app, room_manager, session_cookie_name
from app.room_manager import RoomManager


class FakeSocket:
    """Minimal WebSocket double that records sends and can fail or block."""

    def __init__(self, *, fail_with: Exception | None = None, gate: asyncio.Event | None = None):
        self.fail_with = fail_with
        self.gate = gate
        self.started = asyncio.Event()
        self.sent: list[dict] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def send_json(self, payload: dict) -> None:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            self.started.set()
            if self.gate is not None:
                await self.gate.wait()
            await asyncio.sleep(0)
            if self.fail_with is not None:
                raise self.fail_with
            self.sent.append(payload)
        finally:
            self.in_flight -= 1


def setup_room(*names: str, sockets: list[FakeSocket]):
    manager = RoomManager()
    room = manager.create_room("Delivery")
    players, connection_ids = [], []
    for name, socket in zip(names, sockets, strict=True):
        player = manager.join_room(room.id, name).player
        connection_id = manager.connect_socket(room.id, socket)
        manager.attach_player_to_connection(room.id, connection_id, player.id)
        players.append(player)
        connection_ids.append(connection_id)
    return manager, room, players, connection_ids


def registry(manager: RoomManager, room_id: str) -> dict:
    return manager.get_cached_room(room_id).connections


def test_failed_send_does_not_abort_other_players_and_is_cleaned_up() -> None:
    dead = FakeSocket(fail_with=RuntimeError("socket closed"))
    alive = FakeSocket()
    manager, room, _, (dead_id, alive_id) = setup_room("Avery", "Blake", sockets=[dead, alive])

    asyncio.run(manager.broadcast_room_state(room.id))

    assert [m["type"] for m in alive.sent] == ["room_state"]
    assert dead_id not in registry(manager, room.id)
    assert alive_id in registry(manager, room.id)


def test_failed_chat_send_does_not_abort_other_players() -> None:
    dead = FakeSocket(fail_with=RuntimeError("socket closed"))
    alive = FakeSocket()
    manager, room, _, (dead_id, _) = setup_room("Avery", "Blake", sockets=[dead, alive])

    asyncio.run(manager.broadcast_room_message(room.id, "Avery", "hi"))

    assert alive.sent == [{"type": "chat", "author": "Avery", "message": "hi"}]
    assert dead_id not in registry(manager, room.id)


def test_failed_old_send_never_removes_replacement_connection() -> None:
    async def scenario():
        gate = asyncio.Event()
        old = FakeSocket(fail_with=RuntimeError("stale"), gate=gate)
        manager, room, (player,), (old_id,) = setup_room("Avery", sockets=[old])

        broadcast = asyncio.create_task(manager.broadcast_room_state(room.id))
        await old.started.wait()

        # Player reconnects while the old socket's send is still in flight.
        new = FakeSocket()
        new_id = manager.connect_socket(room.id, new)
        manager.attach_player_to_connection(room.id, new_id, player.id)
        assert list(registry(manager, room.id)) == [new_id]

        gate.set()
        await broadcast
        assert list(registry(manager, room.id)) == [new_id]

        await manager.broadcast_room_state(room.id)
        assert [m["type"] for m in new.sent] == ["room_state"]

    asyncio.run(scenario())


def test_connection_removed_during_broadcast_is_skipped() -> None:
    async def scenario():
        leaving, staying = FakeSocket(), FakeSocket()
        manager, room, _, (leaving_id, _) = setup_room(
            "Avery", "Blake", sockets=[leaving, staying]
        )
        # Hold the leaving socket's send lock so its delivery is pending when it disconnects.
        lock = manager._send_lock(leaving)
        await lock.acquire()
        broadcast = asyncio.create_task(manager.broadcast_room_state(room.id))
        await staying.started.wait()

        manager.disconnect_socket(room.id, leaving_id)
        lock.release()
        await broadcast

        assert leaving.sent == []
        assert len(staying.sent) == 1

    asyncio.run(scenario())


def test_room_eviction_during_broadcast_does_not_raise() -> None:
    async def scenario():
        gate = asyncio.Event()
        slow = FakeSocket(gate=gate)
        manager, room, _, _ = setup_room("Avery", sockets=[slow])

        broadcast = asyncio.create_task(manager.broadcast_room_state(room.id))
        await slow.started.wait()
        manager._evict_room(room.id)
        gate.set()
        await broadcast

        assert manager.get_cached_room(room.id) is None

    asyncio.run(scenario())


def test_concurrent_broadcasts_never_overlap_on_one_socket() -> None:
    async def scenario():
        socket = FakeSocket()
        manager, room, (player,), _ = setup_room("Avery", sockets=[socket])

        await asyncio.gather(
            *(manager.broadcast_room_state(room.id) for _ in range(10)),
            *(manager.broadcast_room_message(room.id, "Avery", str(i)) for i in range(10)),
            *(manager.send_json(socket, {"type": "error", "message": "x"}) for _ in range(5)),
        )

        assert socket.max_in_flight == 1
        assert len(socket.sent) == 25

    asyncio.run(scenario())


def test_latest_state_is_delivered_last_when_broadcasts_race() -> None:
    async def scenario():
        socket = FakeSocket()
        manager, room, (player,), _ = setup_room("Avery", sockets=[socket])

        first = asyncio.create_task(manager.broadcast_room_state(room.id))
        await socket.started.wait()
        manager.set_ready(room.id, player.id, True)
        second = asyncio.create_task(manager.broadcast_room_state(room.id))
        await asyncio.gather(first, second)

        assert socket.sent[-1]["room"]["players"][0]["ready"] is True

    asyncio.run(scenario())


def test_real_websocket_survives_peer_disconnect_during_activity() -> None:
    http = TestClient(
        app,
        base_url="https://testserver",
        headers={"origin": "http://localhost:5173"},
    )
    created = http.post("/rooms", json={"name": "Peers"}, headers={"Idempotency-Key": str(uuid4())})
    room_id = created.json()["room_id"]
    cookies = {}
    for name in ("Avery", "Blake"):
        peer = TestClient(
            app,
            base_url="https://testserver",
            headers={"origin": "http://localhost:5173"},
        )
        nonce = name[0].lower() * 64
        digest = sha256(f"join-v1\0{room_id}\0{name}\0{nonce}".encode()).hexdigest()
        key = f"j1.{nonce}.{digest}"
        response = peer.post(
            f"/rooms/{room_id}/players", json={"name": name}, headers={"Idempotency-Key": key}
        )
        assert response.status_code == 201
        cookies[name] = peer

    def connect(peer: TestClient):
        token = peer.cookies.get(session_cookie_name(room_id))
        return peer.websocket_connect(
            f"/ws/rooms/{room_id}",
            headers={
                "origin": "http://localhost:5173",
                "cookie": f"{session_cookie_name(room_id)}={token}",
            },
        )

    with connect(cookies["Blake"]) as blake:
        assert blake.receive_json()["type"] == "room_state"
        with connect(cookies["Avery"]) as avery:
            assert avery.receive_json()["type"] == "room_state"
            assert blake.receive_json()["type"] == "room_state"
        # Avery has disconnected; Blake must still receive updates.
        assert blake.receive_json()["type"] == "room_state"
        blake.send_json({"type": "toggle_ready", "ready": True})
        update = blake.receive_json()
        assert update["type"] == "room_state"
        assert {p["name"]: p["ready"] for p in update["room"]["players"]}["Blake"] is True

    assert room_manager.get_cached_room(room_id) is not None
