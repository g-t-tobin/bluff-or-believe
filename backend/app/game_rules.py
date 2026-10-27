from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


class GameRulesError(ValueError):
    """Raised when a game action violates the rule set."""


@dataclass(slots=True)
class Player:
    id: str
    name: str
    score: int = 0


@dataclass(slots=True)
class Round:
    prompt: str
    speaker_id: str
    statement: str
    truth_value: bool
    belief_votes: dict[str, bool] = field(default_factory=dict)
    resolved: bool = False


@dataclass(slots=True)
class GameState:
    players: dict[str, Player]
    rounds: list[Round] = field(default_factory=list)

    @classmethod
    def from_player_names(cls, names: Sequence[str]) -> GameState:
        normalized_names = _normalize_player_names(names)
        players = {
            f"player-{index}": Player(id=f"player-{index}", name=name)
            for index, name in enumerate(normalized_names, start=1)
        }
        return cls(players=players)

    @property
    def current_round(self) -> Round | None:
        if not self.rounds:
            return None
        last_round = self.rounds[-1]
        if last_round.resolved:
            return None
        return last_round

    def start_round(
        self,
        *,
        prompt: str,
        speaker_id: str,
        statement: str,
        truth_value: bool,
    ) -> Round:
        if self.current_round is not None:
            raise GameRulesError("Finish the current round before starting another one.")

        if speaker_id not in self.players:
            raise GameRulesError(f"Unknown speaker '{speaker_id}'.")

        if not prompt.strip():
            raise GameRulesError("The prompt cannot be empty.")

        if not statement.strip():
            raise GameRulesError("The statement cannot be empty.")

        round_state = Round(
            prompt=prompt.strip(),
            speaker_id=speaker_id,
            statement=statement.strip(),
            truth_value=truth_value,
        )
        self.rounds.append(round_state)
        return round_state

    def cast_vote(self, voter_id: str, *, believed: bool) -> None:
        active_round = self.current_round
        if active_round is None:
            raise GameRulesError("There is no active round to vote on.")

        if voter_id not in self.players:
            raise GameRulesError(f"Unknown player '{voter_id}'.")

        if voter_id == active_round.speaker_id:
            raise GameRulesError("The speaker cannot vote on their own statement.")

        if voter_id in active_round.belief_votes:
            raise GameRulesError(f"Player '{voter_id}' has already voted.")

        active_round.belief_votes[voter_id] = believed

    def resolve_round(self) -> dict[str, int]:
        active_round = self.current_round
        if active_round is None:
            raise GameRulesError("There is no active round to resolve.")

        expected_voters = {
            player_id for player_id in self.players if player_id != active_round.speaker_id
        }
        if set(active_round.belief_votes) != expected_voters:
            raise GameRulesError(
                "All players except the speaker must vote before resolving the round."
            )

        correct_votes = 0
        for voted_truthfully in active_round.belief_votes.values():
            if voted_truthfully == active_round.truth_value:
                correct_votes += 1

        for voter_id, vote in active_round.belief_votes.items():
            if vote == active_round.truth_value:
                self.players[voter_id].score += 2

        self.players[active_round.speaker_id].score += correct_votes * 2
        active_round.resolved = True

        return {player_id: player.score for player_id, player in self.players.items()}

    def leaderboard(self) -> list[tuple[str, int, str]]:
        ranking = sorted(
            self.players.values(),
            key=lambda player: (-player.score, player.name.lower()),
        )
        return [(player.id, player.score, player.name) for player in ranking]


def _normalize_player_names(names: Sequence[str]) -> list[str]:
    normalized = [name.strip() for name in names]
    if not 2 <= len(normalized) <= 8:
        raise GameRulesError("A game must have between 2 and 8 players.")

    if any(not name for name in normalized):
        raise GameRulesError("Player names cannot be blank.")

    if len(set(normalized)) != len(normalized):
        raise GameRulesError("Player names must be unique.")

    return normalized
