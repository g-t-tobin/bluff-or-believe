import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app, room_manager, session_cookie_name
from app.room_manager import RoomAuthorizationError

client = TestClient(
    app,
    base_url="https://testserver",
    headers={"origin": "http://localhost:5173"},
)


def websocket_headers(test_client: TestClient, room_id: str) -> dict[str, str]:
    token = test_client.cookies.get(session_cookie_name(room_id))
    assert token
    return {
        "origin": "http://localhost:5173",
        "cookie": f"{session_cookie_name(room_id)}={token}",
    }


CLOUDFRONT_ORIGIN = "https://d12w3y8n7g6dw9.cloudfront.net"


def cloudfront_client() -> TestClient:
    return TestClient(
        app,
        base_url=CLOUDFRONT_ORIGIN,
        headers={"origin": CLOUDFRONT_ORIGIN},
    )


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
    assert "session_token" not in player_response.json()
    assert "httponly" in player_response.headers["set-cookie"].lower()
    cookie_header = player_response.headers["set-cookie"].lower()
    assert "secure" in cookie_header
    assert "httponly" in cookie_header
    assert "samesite=lax" in cookie_header
    assert "__host-" in cookie_header

    rejoin_response = client.post(
        f"/rooms/{room['room_id']}/players",
        json={"name": "avery"},
    )
    assert rejoin_response.status_code == 201
    assert rejoin_response.json()["player"]["id"] == player["id"]
    assert "session_token" not in rejoin_response.json()

    stored_room = client.get(f"/rooms/{room['room_id']}")
    assert stored_room.status_code == 200
    assert [player["name"] for player in stored_room.json()["players"]] == ["Avery"]


def test_cloudfront_origin_room_creation_join_and_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.main.get_cors_origins", lambda: [CLOUDFRONT_ORIGIN])
    browser = cloudfront_client()
    room_response = browser.post("/rooms", json={"name": "CloudFront Session"})
    assert room_response.status_code == 201
    room_id = room_response.json()["room_id"]

    join_response = browser.post(
        f"/rooms/{room_id}/players",
        json={"name": "Avery"},
    )
    assert join_response.status_code == 201
    cookie_header = join_response.headers["set-cookie"].lower()
    assert "secure" in cookie_header
    assert "httponly" in cookie_header
    assert "samesite=lax" in cookie_header
    assert "__host-" in cookie_header
    assert "domain=" not in cookie_header

    room_read = browser.get(f"/rooms/{room_id}")
    assert room_read.status_code == 200
    assert room_read.json()["players"][0]["name"] == "Avery"


def test_players_in_separate_browser_sessions_can_join_and_read_same_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.main.get_cors_origins", lambda: [CLOUDFRONT_ORIGIN])
    first_browser = cloudfront_client()
    second_browser = cloudfront_client()

    room_id = first_browser.post("/rooms", json={"name": "Two Browsers"}).json()["room_id"]
    first_join = first_browser.post(
        f"/rooms/{room_id}/players",
        json={"name": "Avery"},
    )
    second_join = second_browser.post(
        f"/rooms/{room_id}/players",
        json={"name": "Blake"},
    )

    assert first_join.status_code == 201
    assert second_join.status_code == 201
    assert first_join.json()["player"]["id"] != second_join.json()["player"]["id"]
    assert first_browser.get(f"/rooms/{room_id}").status_code == 200
    assert second_browser.get(f"/rooms/{room_id}").status_code == 200
    impostor_rejoin = second_browser.post(
        f"/rooms/{room_id}/players",
        json={"name": "Avery"},
    )
    assert impostor_rejoin.status_code == 401
    assert "max-age=0" in impostor_rejoin.headers["set-cookie"].lower()
    assert second_browser.cookies.get(session_cookie_name(room_id)) is None


def test_websocket_broadcasts_room_updates() -> None:
    room_response = client.post("/rooms", json={"name": "Socket Room"})
    room_id = room_response.json()["room_id"]
    joined = client.post(f"/rooms/{room_id}/players", json={"name": "Blake"})
    assert joined.status_code == 201

    with client.websocket_connect(
        f"/ws/rooms/{room_id}",
        headers=websocket_headers(client, room_id),
    ) as websocket:
        first_message = websocket.receive_json()
        assert first_message["type"] == "room_state"
        players = first_message["room"]["players"]
        assert [player["name"] for player in players] == ["Blake"]

        websocket.send_json({"type": "toggle_ready", "ready": True})
        second_message = websocket.receive_json()
        assert second_message["type"] == "room_state"
        assert second_message["room"]["players"][0]["ready"] is True


def test_websocket_accepts_authenticated_cloudfront_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.main.get_cors_origins", lambda: [CLOUDFRONT_ORIGIN])
    browser = cloudfront_client()
    room_id = browser.post("/rooms", json={"name": "CloudFront Socket"}).json()["room_id"]
    joined = browser.post(f"/rooms/{room_id}/players", json={"name": "Avery"})
    assert joined.status_code == 201

    token = browser.cookies.get(session_cookie_name(room_id))
    assert token
    with browser.websocket_connect(
        f"/ws/rooms/{room_id}",
        headers={
            "origin": CLOUDFRONT_ORIGIN,
            "cookie": f"{session_cookie_name(room_id)}={token}",
        },
    ) as websocket:
        message = websocket.receive_json()
        assert message["type"] == "room_state"
        assert message["room"]["players"][0]["name"] == "Avery"


def test_websocket_reconnection_persists_expiry_without_duplicate_player() -> None:
    browser = TestClient(
        app,
        base_url="https://testserver",
        headers={"origin": "http://localhost:5173"},
    )
    room_id = browser.post("/rooms", json={"name": "Persistent Reconnect"}).json()["room_id"]
    joined = browser.post(f"/rooms/{room_id}/players", json={"name": "Avery"})
    assert joined.status_code == 201
    player_id = joined.json()["player"]["id"]

    room = room_manager.get_room(room_id)
    assert room is not None
    room.expires_at = int(time.time()) + 60
    assert room_manager.repository.save(room, expected_version=room.version)

    for _ in range(2):
        with browser.websocket_connect(
            f"/ws/rooms/{room_id}",
            headers=websocket_headers(browser, room_id),
        ) as websocket:
            initial = websocket.receive_json()
            assert initial["room"]["players"][0]["id"] == player_id

    restored = room_manager.get_room(room_id)
    assert restored is not None
    assert len(restored.players) == 1
    assert restored.players[player_id].name == "Avery"
    assert restored.expires_at > int(time.time()) + 60


def test_game_round_starts_and_resolves_scores() -> None:
    room = room_manager.create_room("Game Room")
    player_a = room_manager.join_room(room.id, "Avery").player
    player_b = room_manager.join_room(room.id, "Blake").player
    player_c = room_manager.join_room(room.id, "Casey").player

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


def test_http_room_summaries_hide_votes_until_resolution() -> None:
    clients = [
        TestClient(
            app,
            base_url="https://testserver",
            headers={"origin": "http://localhost:5173"},
        )
        for _ in range(3)
    ]
    room_id = clients[0].post("/rooms", json={"name": "Private Votes"}).json()["room_id"]
    players = [
        client.post(
            f"/rooms/{room_id}/players",
            json={"name": name},
        ).json()["player"]
        for client, name in zip(clients, ("Avery", "Blake", "Casey"), strict=True)
    ]

    started = clients[0].post(f"/rooms/{room_id}/start").json()
    game = started["game"]
    assert game["speaker_id"] == players[0]["id"]
    assert "truth_value" not in game
    assert "votes" not in game
    assert game["has_voted"] is False

    first_vote = clients[1].post(
        f"/rooms/{room_id}/vote",
        json={"belief": True},
    ).json()["game"]
    assert first_vote["has_voted"] is True
    assert "truth_value" not in first_vote
    assert "votes" not in first_vote

    other_player_view = clients[2].get(f"/rooms/{room_id}").json()["game"]
    assert other_player_view["has_voted"] is False
    assert "truth_value" not in other_player_view
    assert "votes" not in other_player_view

    resolved = clients[2].post(
        f"/rooms/{room_id}/vote",
        json={"belief": False},
    ).json()["game"]
    assert resolved["resolved"] is True
    assert isinstance(resolved["truth_value"], bool)
    assert resolved["votes"] == {
        players[1]["id"]: True,
        players[2]["id"]: False,
    }

    resolved_other_view = clients[0].get(f"/rooms/{room_id}").json()["game"]
    assert resolved_other_view["truth_value"] == resolved["truth_value"]
    assert resolved_other_view["votes"] == resolved["votes"]


def test_websocket_room_messages_hide_votes_until_resolution() -> None:
    clients = [
        TestClient(
            app,
            base_url="https://testserver",
            headers={"origin": "http://localhost:5173"},
        )
        for _ in range(3)
    ]
    room_id = clients[0].post("/rooms", json={"name": "Socket Privacy"}).json()["room_id"]
    players = [
        client.post(
            f"/rooms/{room_id}/players",
            json={"name": name},
        ).json()["player"]
        for client, name in zip(clients, ("Avery", "Blake", "Casey"), strict=True)
    ]
    started = clients[0].post(f"/rooms/{room_id}/start").json()["game"]
    assert started["speaker_id"] == players[0]["id"]

    with (
        clients[0].websocket_connect(
            f"/ws/rooms/{room_id}",
            headers=websocket_headers(clients[0], room_id),
        ) as speaker_socket,
        clients[1].websocket_connect(
            f"/ws/rooms/{room_id}",
            headers=websocket_headers(clients[1], room_id),
        ) as first_voter_socket,
        clients[2].websocket_connect(
            f"/ws/rooms/{room_id}",
            headers=websocket_headers(clients[2], room_id),
        ) as second_voter_socket,
    ):
        for _ in range(3):
            speaker_initial = speaker_socket.receive_json()["room"]["game"]
        for _ in range(2):
            first_initial = first_voter_socket.receive_json()["room"]["game"]
        second_initial = second_voter_socket.receive_json()["room"]["game"]
        for game in (speaker_initial, first_initial, second_initial):
            assert "truth_value" not in game
            assert "votes" not in game
        assert speaker_initial["has_voted"] is False
        assert first_initial["has_voted"] is False
        assert second_initial["has_voted"] is False

        first_voter_socket.send_json({"type": "vote", "belief": True})
        active_views = [
            speaker_socket.receive_json()["room"]["game"],
            first_voter_socket.receive_json()["room"]["game"],
            second_voter_socket.receive_json()["room"]["game"],
        ]
        for game in active_views:
            assert "truth_value" not in game
            assert "votes" not in game
        assert [game["has_voted"] for game in active_views] == [False, True, False]

        second_voter_socket.send_json({"type": "vote", "belief": False})
        resolved_views = [
            speaker_socket.receive_json()["room"]["game"],
            first_voter_socket.receive_json()["room"]["game"],
            second_voter_socket.receive_json()["room"]["game"],
        ]

    for game in resolved_views:
        assert game["resolved"] is True
        assert isinstance(game["truth_value"], bool)
        assert game["votes"] == {
            players[1]["id"]: True,
            players[2]["id"]: False,
        }


def test_room_start_and_vote_endpoints() -> None:
    owner_client = TestClient(
        app,
        base_url="https://testserver",
        headers={"origin": "http://localhost:5173"},
    )
    voter_client = TestClient(
        app,
        base_url="https://testserver",
        headers={"origin": "http://localhost:5173"},
    )
    room_response = owner_client.post("/rooms", json={"name": "Vote Room"})
    room_id = room_response.json()["room_id"]
    player_one = owner_client.post(
        f"/rooms/{room_id}/players",
        json={"name": "Avery"},
    ).json()["player"]
    voter_client.post(
        f"/rooms/{room_id}/players",
        json={"name": "Blake"},
    ).json()["player"]

    started = owner_client.post(f"/rooms/{room_id}/start")
    assert started.status_code == 200
    assert "game" in started.json()

    voter = (
        voter_client
        if started.json()["game"]["speaker_id"] == player_one["id"]
        else owner_client
    )
    vote_response = voter.post(
        f"/rooms/{room_id}/vote",
        json={"belief": True},
    )
    assert vote_response.status_code == 200
    assert "scores" in vote_response.json()

    assert player_one["id"] in vote_response.json()["scores"]


def test_rejoining_with_same_name_uses_existing_player() -> None:
    room = room_manager.create_room("Reconnect Room")
    first_join = room_manager.join_room(room.id, "Avery")

    with pytest.raises(ValueError, match="already in use"):
        room_manager.join_room(room.id, "avery")

    second_join = room_manager.join_room(room.id, "avery", first_join.session_token)
    assert second_join.player.id == first_join.player.id
    assert second_join.session_token is None
    restored_room = room_manager.get_room(room.id)
    assert restored_room is not None
    assert len(restored_room.players) == 1


def test_expired_player_session_is_rejected() -> None:
    room = room_manager.create_room("Expired Session")
    joined = room_manager.join_room(room.id, "Avery")
    expired_room = room_manager.get_room(room.id)
    assert expired_room is not None
    expired_room.players[joined.player.id].session_expires_at = time.time() - 1
    assert room_manager.repository.save(expired_room, expected_version=expired_room.version)

    assert room_manager.authenticate_session(room.id, joined.session_token) is None


def test_player_session_is_bound_to_its_room_and_name() -> None:
    room = room_manager.create_room("Bound Session")
    other_room = room_manager.create_room("Other Room")
    joined = room_manager.join_room(room.id, "Avery")

    assert room_manager.authenticate_session(other_room.id, joined.session_token) is None
    with pytest.raises(RoomAuthorizationError):
        room_manager.join_room(room.id, "Blake", joined.session_token)


def test_reconnect_keeps_one_active_connection_per_player() -> None:
    room = room_manager.create_room("Reconnect Stability")
    player = room_manager.join_room(room.id, "Avery").player

    first_connection = room_manager.connect_socket(room.id, object())
    second_connection = room_manager.connect_socket(room.id, object())

    room_manager.attach_player_to_connection(room.id, first_connection, player.id)
    room_manager.attach_player_to_connection(room.id, second_connection, player.id)

    connected_room = room_manager.get_room(room.id)
    assert connected_room is not None
    active_connections = [
        connection_id
        for connection_id, connection in connected_room.connections.items()
        if connection.get("player_id") == player.id
    ]

    assert active_connections == [second_connection]
    assert len(connected_room.connections) == 1


def test_duplicate_vote_is_rejected_and_round_remains_consistent() -> None:
    room = room_manager.create_room("Vote Guard")
    speaker = room_manager.join_room(room.id, "Avery").player
    voter_one = room_manager.join_room(room.id, "Blake").player
    voter_two = room_manager.join_room(room.id, "Casey").player

    round_state = room_manager.start_round(room.id)
    assert round_state.speaker_id == speaker.id

    room_manager.cast_vote(room.id, voter_one.id, True)

    try:
        room_manager.cast_vote(room.id, voter_one.id, False)
        raise AssertionError("Duplicate vote should raise ValueError")
    except ValueError as exc:
        assert "already voted" in str(exc)

    updated_room = room_manager.get_room(room.id)
    assert updated_room is not None and updated_room.game is not None
    assert len(updated_room.game.votes) == 1
    assert voter_one.id in updated_room.game.votes
    assert voter_two.id not in updated_room.game.votes

    room_manager.cast_vote(room.id, voter_two.id, False)
    final_room = room_manager.get_room(room.id)
    assert final_room is not None and final_room.game is not None
    assert len(final_room.game.votes) == 2


def test_room_reads_and_actions_require_a_valid_session() -> None:
    room_response = client.post("/rooms", json={"name": "Private Table"})
    room_id = room_response.json()["room_id"]

    assert client.get(f"/rooms/{room_id}").status_code == 401
    assert client.post(f"/rooms/{room_id}/start").status_code == 401
    assert client.post(f"/rooms/{room_id}/vote", json={"belief": True}).status_code == 401


def test_expired_room_read_returns_401_and_clears_browser_cookie() -> None:
    browser = TestClient(
        app,
        base_url="https://testserver",
        headers={"origin": "http://localhost:5173"},
    )
    room_id = browser.post("/rooms", json={"name": "Expired HTTP Session"}).json()["room_id"]
    joined = browser.post(f"/rooms/{room_id}/players", json={"name": "Avery"})
    assert joined.status_code == 201
    player_id = joined.json()["player"]["id"]
    expired_room = room_manager.get_room(room_id)
    assert expired_room is not None
    expired_room.players[player_id].session_expires_at = time.time() - 1
    assert room_manager.repository.save(expired_room, expected_version=expired_room.version)

    expired_read = browser.get(f"/rooms/{room_id}")

    assert expired_read.status_code == 401
    set_cookie = expired_read.headers["set-cookie"].lower()
    assert session_cookie_name(room_id).lower() in set_cookie
    assert "max-age=0" in set_cookie
    assert browser.cookies.get(session_cookie_name(room_id)) is None


def test_missing_room_read_returns_404_and_clears_room_cookie() -> None:
    browser = TestClient(
        app,
        base_url="https://testserver",
        headers={"origin": "http://localhost:5173"},
    )
    room_id = f"missing-{uuid4().hex}"
    browser.cookies.set(session_cookie_name(room_id), "stale-session")
    missing_read = browser.get(f"/rooms/{room_id}")

    assert missing_read.status_code == 404
    assert "max-age=0" in missing_read.headers["set-cookie"].lower()


def test_cookie_authentication_requires_same_site_origin_on_http_mutations() -> None:
    room_id = client.post("/rooms", json={"name": "Origin Check"}).json()["room_id"]
    joined = client.post(f"/rooms/{room_id}/players", json={"name": "Avery"})
    assert joined.status_code == 201

    assert client.post(
        f"/rooms/{room_id}/start",
        headers={"origin": "https://attacker.example"},
    ).status_code == 403
    assert client.post(
        f"/rooms/{room_id}/vote",
        json={"belief": True},
        headers={"origin": ""},
    ).status_code == 403

    attacker_client = TestClient(
        app,
        base_url="https://testserver",
        headers={"origin": "https://attacker.example"},
    )
    other_room = client.post("/rooms", json={"name": "Join Origin Check"}).json()
    rejected_join = attacker_client.post(
        f"/rooms/{other_room['room_id']}/players",
        json={"name": "Avery"},
    )
    assert rejected_join.status_code == 403
    assert "set-cookie" not in rejected_join.headers


def test_invalid_session_cannot_access_room_or_websocket() -> None:
    room_response = client.post("/rooms", json={"name": "Invalid Session"})
    room_id = room_response.json()["room_id"]
    cookie_name = f"__Host-player_session_{room_id}"
    client.cookies.set(cookie_name, "not-a-valid-session")

    assert client.get(f"/rooms/{room_id}").status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/ws/rooms/{room_id}",
            headers={
                "origin": "http://localhost:5173",
                "cookie": f"{cookie_name}=not-a-valid-session",
            },
        ):
            pass


def test_player_cannot_claim_an_existing_name_or_supply_another_player_id() -> None:
    first_client = TestClient(
        app,
        base_url="https://testserver",
        headers={"origin": "http://localhost:5173"},
    )
    second_client = TestClient(
        app,
        base_url="https://testserver",
        headers={"origin": "http://localhost:5173"},
    )
    room_response = first_client.post("/rooms", json={"name": "Identity Table"})
    room_id = room_response.json()["room_id"]

    first_player = first_client.post(
        f"/rooms/{room_id}/players",
        json={"name": "Avery"},
    ).json()["player"]
    duplicate = second_client.post(
        f"/rooms/{room_id}/players",
        json={"name": "Avery"},
    )
    assert duplicate.status_code == 409

    assert second_client.post(
        f"/rooms/{room_id}/players",
        json={"name": "Blake"},
    ).status_code == 201
    started = first_client.post(f"/rooms/{room_id}/start")
    assert started.status_code == 200

    if started.json()["game"]["speaker_id"] == first_player["id"]:
        voter_client = second_client
    else:
        voter_client = first_client
    vote = voter_client.post(
        f"/rooms/{room_id}/vote",
        json={"belief": True, "player_id": first_player["id"]},
    )
    assert vote.status_code == 422


def test_websocket_rejects_an_unauthenticated_connection() -> None:
    room_response = client.post("/rooms", json={"name": "No Session"})
    room_id = room_response.json()["room_id"]
    unauthenticated_client = TestClient(
        app,
        base_url="https://testserver",
        headers={"origin": "http://localhost:5173"},
    )

    with pytest.raises(WebSocketDisconnect):
        with unauthenticated_client.websocket_connect(f"/ws/rooms/{room_id}"):
            pass


def test_websocket_requires_an_allowed_origin() -> None:
    room_id = client.post("/rooms", json={"name": "WebSocket Origin"}).json()["room_id"]
    assert client.post(f"/rooms/{room_id}/players", json={"name": "Avery"}).status_code == 201
    headers = websocket_headers(client, room_id)
    headers["origin"] = "https://attacker.example"

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/rooms/{room_id}", headers=headers):
            pass


def test_websocket_vote_uses_authenticated_player_not_submitted_id() -> None:
    speaker_client = TestClient(
        app,
        base_url="https://testserver",
        headers={"origin": "http://localhost:5173"},
    )
    voter_client = TestClient(
        app,
        base_url="https://testserver",
        headers={"origin": "http://localhost:5173"},
    )
    room = speaker_client.post("/rooms", json={"name": "Bound Vote"}).json()
    room_id = room["room_id"]
    speaker = speaker_client.post(
        f"/rooms/{room_id}/players",
        json={"name": "Avery"},
    ).json()["player"]
    voter = voter_client.post(
        f"/rooms/{room_id}/players",
        json={"name": "Blake"},
    ).json()["player"]

    started = speaker_client.post(f"/rooms/{room_id}/start").json()
    assert started["game"]["speaker_id"] == speaker["id"]

    with voter_client.websocket_connect(
        f"/ws/rooms/{room_id}",
        headers=websocket_headers(voter_client, room_id),
    ) as websocket:
        websocket.receive_json()
        websocket.send_json(
            {
                "type": "vote",
                "belief": True,
                "player_id": speaker["id"],
            }
        )
        updated = websocket.receive_json()["room"]

    assert updated["game"]["votes"] == {voter["id"]: True}
