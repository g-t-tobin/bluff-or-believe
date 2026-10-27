from fastapi.testclient import TestClient

from app.main import app, room_manager

client = TestClient(app)


def test_room_creation_and_player_join() -> None:
    room_response = client.post("/rooms", json={"name": "Table 1"})

    assert room_response.status_code == 201
    room = room_response.json()
    assert room["name"] == "Table 1"
    assert "room_id" in room

    player_response = client.post(
        f"/rooms/{room['room_id']}/players",
        json={"name": "Avery"},
    )

    assert player_response.status_code == 201
    player = player_response.json()["player"]
    assert player["name"] == "Avery"
    assert player["ready"] is False

    stored_room = client.get(f"/rooms/{room['room_id']}")
    assert stored_room.status_code == 200
    assert [player["name"] for player in stored_room.json()["players"]] == ["Avery"]


def test_websocket_broadcasts_room_updates() -> None:
    room_response = client.post("/rooms", json={"name": "Socket Room"})
    room_id = room_response.json()["room_id"]

    with client.websocket_connect(f"/ws/rooms/{room_id}?player_name=Blake") as websocket:
        first_message = websocket.receive_json()
        assert first_message["type"] == "room_state"
        players = first_message["room"]["players"]
        assert [player["name"] for player in players] == ["Blake"]

        websocket.send_json({"type": "toggle_ready", "ready": True})
        second_message = websocket.receive_json()
        assert second_message["type"] == "room_state"
        assert second_message["room"]["players"][0]["ready"] is True


def test_game_round_starts_and_resolves_scores() -> None:
    room = room_manager.create_room("Game Room")
    player_a = room_manager.join_room(room.id, "Avery")
    player_b = room_manager.join_room(room.id, "Blake")
    player_c = room_manager.join_room(room.id, "Casey")

    round_state = room_manager.start_round(room.id)
    assert round_state.round_number == 1
    assert round_state.speaker_id in {player_a.id, player_b.id, player_c.id}

    non_speaker_ids = [
        player.id
        for player in (player_a, player_b, player_c)
        if player.id != round_state.speaker_id
    ]
    for player_id in non_speaker_ids:
        room_manager.cast_vote(room.id, player_id, round_state.truth_value)

    room_summary = room_manager.room_summary(room.id)
    game = room_summary["game"]
    assert game["resolved"] is True
    assert room_summary["scores"][round_state.speaker_id] >= 0
    assert sum(room_summary["scores"].values()) >= 2


def test_room_start_and_vote_endpoints() -> None:
    room_response = client.post("/rooms", json={"name": "Vote Room"})
    room_id = room_response.json()["room_id"]
    player_one = client.post(f"/rooms/{room_id}/players", json={"name": "Avery"}).json()["player"]
    player_two = client.post(f"/rooms/{room_id}/players", json={"name": "Blake"}).json()["player"]

    started = client.post(f"/rooms/{room_id}/start")
    assert started.status_code == 200
    assert "game" in started.json()

    vote_response = client.post(
        f"/rooms/{room_id}/vote",
        json={"player_id": player_two["id"], "belief": True},
    )
    assert vote_response.status_code == 200
    assert "scores" in vote_response.json()

    assert player_one["id"] in vote_response.json()["scores"]


def test_rejoining_with_same_name_uses_existing_player() -> None:
    room = room_manager.create_room("Reconnect Room")
    first_join = room_manager.join_room(room.id, "Avery")
    second_join = room_manager.join_room(room.id, "avery")

    assert second_join.id == first_join.id
    assert len(room.players) == 1


def test_reconnect_keeps_one_active_connection_per_player() -> None:
    room = room_manager.create_room("Reconnect Stability")
    player = room_manager.join_room(room.id, "Avery")

    first_connection = room_manager.connect_socket(room.id, object())
    second_connection = room_manager.connect_socket(room.id, object())

    room_manager.attach_player_to_connection(room.id, first_connection, player.id)
    room_manager.attach_player_to_connection(room.id, second_connection, player.id)

    active_connections = [
        connection_id
        for connection_id, connection in room.connections.items()
        if connection.get("player_id") == player.id
    ]

    assert active_connections == [second_connection]
    assert len(room.connections) == 1


def test_duplicate_vote_is_rejected_and_round_remains_consistent() -> None:
    room = room_manager.create_room("Vote Guard")
    speaker = room_manager.join_room(room.id, "Avery")
    voter_one = room_manager.join_room(room.id, "Blake")
    voter_two = room_manager.join_room(room.id, "Casey")

    room.game = room_manager.start_round(room.id)
    room.game.speaker_id = speaker.id

    room_manager.cast_vote(room.id, voter_one.id, True)

    try:
        room_manager.cast_vote(room.id, voter_one.id, False)
        raise AssertionError("Duplicate vote should raise ValueError")
    except ValueError as exc:
        assert "already voted" in str(exc)

    assert len(room.game.votes) == 1
    assert voter_one.id in room.game.votes
    assert voter_two.id not in room.game.votes

    room_manager.cast_vote(room.id, voter_two.id, False)
    assert len(room.game.votes) == 2
