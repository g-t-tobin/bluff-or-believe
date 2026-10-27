import pytest

from app.game_rules import GameRulesError, GameState


def test_game_requires_between_two_and_eight_players() -> None:
    with pytest.raises(GameRulesError, match="between 2 and 8 players"):
        GameState.from_player_names(["Solo"])

    with pytest.raises(GameRulesError, match="between 2 and 8 players"):
        GameState.from_player_names([f"Player {index}" for index in range(9)])


def test_round_resolution_awards_correct_votes_and_speaker_points() -> None:
    game = GameState.from_player_names(["Avery", "Blake", "Casey", "Drew"])

    game.start_round(
        prompt="Which player is bluffing?",
        speaker_id="player-1",
        statement="I have never seen snow.",
        truth_value=False,
    )

    game.cast_vote("player-2", believed=True)
    game.cast_vote("player-3", believed=False)
    game.cast_vote("player-4", believed=False)

    scores = game.resolve_round()

    assert scores == {
        "player-1": 4,
        "player-2": 0,
        "player-3": 2,
        "player-4": 2,
    }


def test_speaker_cannot_vote_and_players_cannot_vote_twice() -> None:
    game = GameState.from_player_names(["Avery", "Blake", "Casey"])
    game.start_round(
        prompt="Who is telling the truth?",
        speaker_id="player-1",
        statement="I won a marathon last year.",
        truth_value=True,
    )

    with pytest.raises(GameRulesError, match="speaker cannot vote"):
        game.cast_vote("player-1", believed=True)

    game.cast_vote("player-2", believed=True)

    with pytest.raises(GameRulesError, match="already voted"):
        game.cast_vote("player-2", believed=False)


def test_round_must_be_complete_before_resolution() -> None:
    game = GameState.from_player_names(["Avery", "Blake", "Casey"])
    game.start_round(
        prompt="Who is bluffing?",
        speaker_id="player-1",
        statement="I can juggle three flaming torches.",
        truth_value=False,
    )

    with pytest.raises(GameRulesError, match="All players except the speaker must vote"):
        game.resolve_round()
