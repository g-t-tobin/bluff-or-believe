import asyncio

from app.room_manager import RoomManager


class RecordingSocket:
    """WebSocket stand-in that records every message delivered to a player."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)


def test_ten_players_can_join_play_a_round_and_receive_shared_state() -> None:
    async def scenario() -> None:
        manager = RoomManager()
        room = manager.create_room("Ten-player test room")
        names = [f"Player {number:02d}" for number in range(1, 11)]
        players = []
        sockets: list[RecordingSocket] = []

        # Ten distinct players, each with their own socket connection.
        for name in names:
            player = manager.join_room(room.id, name).player
            socket = RecordingSocket()
            connection_id = manager.connect_socket(room.id, socket)
            manager.attach_player_to_connection(room.id, connection_id, player.id)
            players.append(player)
            sockets.append(socket)

        await manager.broadcast_room_state(room.id)

        # Every connected player should see all ten participants.
        for socket in sockets:
            assert socket.messages
            state = socket.messages[-1]
            assert state["type"] == "room_state"
            assert len(state["room"]["players"]) == 10
            assert {item["name"] for item in state["room"]["players"]} == set(names)

        # Simulate everyone toggling ready and verify the update reaches all clients.
        for player in players:
            manager.set_ready(room.id, player.id, True)
        await manager.broadcast_room_state(room.id)

        for socket in sockets:
            assert all(player["ready"] for player in socket.messages[-1]["room"]["players"])

        # Start one round. The speaker cannot vote; the other nine players can.
        round_state = manager.start_round(room.id)
        await manager.broadcast_room_state(room.id)
        assert round_state.speaker_id in {player.id for player in players}

        for socket in sockets:
            game = socket.messages[-1]["room"]["game"]
            assert game["round_number"] == 1
            assert game["speaker_id"] == round_state.speaker_id
            assert game["resolved"] is False

        votes_cast = 0
        for player in players:
            if player.id == round_state.speaker_id:
                continue
            manager.cast_vote(room.id, player.id, votes_cast % 2 == 0)
            votes_cast += 1
            await manager.broadcast_room_state(room.id)

        assert votes_cast == 9
        assert round_state.resolved is True

        # All ten clients receive the resolved result, including the nine votes.
        for socket in sockets:
            final_state = socket.messages[-1]
            assert final_state["type"] == "room_state"
            game = final_state["room"]["game"]
            assert game["round_number"] == 1
            assert game["resolved"] is True
            assert isinstance(game["truth_value"], bool)
            assert len(game["votes"]) == 9
            assert set(game["votes"]) == {
                player.id for player in players if player.id != round_state.speaker_id
            }

        # A chat message should also fan out to every connected player.
        await manager.broadcast_room_message(room.id, names[0], "Hello from Player 01")
        for socket in sockets:
            assert socket.messages[-1] == {
                "type": "chat",
                "author": names[0],
                "message": "Hello from Player 01",
            }

    asyncio.run(scenario())
