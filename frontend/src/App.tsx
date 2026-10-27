import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  joinRoom,
  createJoinIdempotencyKey,
  restoreRoomSession,
  socketCloseMessage,
  type SessionRoom,
  type StoredRoomSession,
} from "./sessionRestore";

type Room = SessionRoom;

const normalizeBaseUrl = (value: string | undefined, fallback: string): string => {
  const candidate = (value ?? fallback).trim();
  if (!candidate) {
    return fallback;
  }

  return candidate.replace(/\/+$/, "");
};

const API_BASE = normalizeBaseUrl(
  import.meta.env.VITE_API_BASE_URL,
  import.meta.env.PROD ? "/api" : "http://localhost:8000",
);
const WS_BASE = normalizeBaseUrl(
  import.meta.env.VITE_WS_BASE_URL,
  import.meta.env.PROD ? "" : "ws://localhost:8000",
);
const LAST_SESSION_KEY = "bluff-or-believe-session";
const ROOM_CREATE_ATTEMPT_KEY = "bluff-or-believe-room-create-attempt";
const ROOM_JOIN_ATTEMPT_KEY = "bluff-or-believe-room-join-attempt";

type RoomCreateAttempt = { name: string; key: string };
type RoomJoinAttempt = { roomId: string; name: string; key: string };

function readRoomCreateAttempt(): RoomCreateAttempt | null {
  const raw = window.localStorage.getItem(ROOM_CREATE_ATTEMPT_KEY);
  if (!raw) {
    return null;
  }
  try {
    const parsed = JSON.parse(raw) as Partial<RoomCreateAttempt>;
    if (typeof parsed.name === "string" && typeof parsed.key === "string") {
      return { name: parsed.name, key: parsed.key };
    }
  } catch {
    window.localStorage.removeItem(ROOM_CREATE_ATTEMPT_KEY);
    return null;
  }
  window.localStorage.removeItem(ROOM_CREATE_ATTEMPT_KEY);
  return null;
}

function readRoomJoinAttempt(): RoomJoinAttempt | null {
  const raw = window.localStorage.getItem(ROOM_JOIN_ATTEMPT_KEY);
  if (!raw) {
    return null;
  }
  try {
    const parsed = JSON.parse(raw) as Partial<RoomJoinAttempt>;
    if (
      typeof parsed.roomId === "string" &&
      typeof parsed.name === "string" &&
      typeof parsed.key === "string"
    ) {
      return { roomId: parsed.roomId, name: parsed.name, key: parsed.key };
    }
  } catch {
    window.localStorage.removeItem(ROOM_JOIN_ATTEMPT_KEY);
    return null;
  }
  window.localStorage.removeItem(ROOM_JOIN_ATTEMPT_KEY);
  return null;
}

function readStoredSession(): StoredRoomSession | null {
  if (typeof window === "undefined") {
    return null;
  }

  try {
    const raw = window.localStorage.getItem(LAST_SESSION_KEY);
    if (!raw) {
      return null;
    }

    const parsed = JSON.parse(raw) as Partial<StoredRoomSession>;
    const roomId = parsed.room_id?.trim();
    const playerName = parsed.player_name?.trim();

    if (!roomId || !playerName) {
      window.localStorage.removeItem(LAST_SESSION_KEY);
      return null;
    }

    return { room_id: roomId, player_name: playerName };
  } catch {
    window.localStorage.removeItem(LAST_SESSION_KEY);
    return null;
  }
}

function writeStoredSession(roomId: string, playerName: string): void {
  if (typeof window === "undefined") {
    return;
  }

  window.localStorage.setItem(
    LAST_SESSION_KEY,
    JSON.stringify({ room_id: roomId, player_name: playerName }),
  );
}

function clearStoredSession(): void {
  if (typeof window === "undefined") {
    return;
  }

  window.localStorage.removeItem(LAST_SESSION_KEY);
}

async function readRoom(roomId: string): Promise<Room> {
  const response = await fetch(`${API_BASE}/rooms/${roomId}`, { credentials: "include" });
  if (!response.ok) {
    throw new Error("Unable to load this room right now.");
  }

  return response.json() as Promise<Room>;
}

async function createRoom(name: string, idempotencyKey: string): Promise<Room> {
  const response = await fetch(`${API_BASE}/rooms`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify({ name }),
  });

  if (!response.ok) {
    const payload = (await response.json().catch(() => ({ detail: "Failed to create room." }))) as {
      detail?: string;
    };
    throw new Error(payload.detail ?? "Failed to create room.");
  }

  return response.json() as Promise<Room>;
}

export default function App() {
  const [roomName, setRoomName] = useState("");
  const [joinRoomId, setJoinRoomId] = useState("");
  const [joinName, setJoinName] = useState("");
  const [room, setRoom] = useState<Room | null>(null);
  const [activePlayerId, setActivePlayerId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [isBusy, setIsBusy] = useState(false);
  const [restoreUnavailable, setRestoreUnavailable] = useState(false);
  const [votingInFlight, setVotingInFlight] = useState(false);
  const [connectionStatus, setConnectionStatus] = useState<
    "idle" | "connecting" | "connected" | "disconnected" | "rejected"
  >("idle");
  const socketRef = useRef<WebSocket | null>(null);
  const socketErrorRef = useRef<string | null>(null);
  const createRequestRef = useRef<{ name: string; key: string } | null>(null);
  const joinRequestRef = useRef<RoomJoinAttempt | null>(null);
  const restoreAttemptedRef = useRef(false);

  const myPlayer = useMemo(
    () => room?.players.find((player) => player.id === activePlayerId) ?? null,
    [activePlayerId, room],
  );

  const currentSpeaker = room?.players.find((player) => player.id === room.game?.speaker_id) ?? null;
  const scoreMap = room?.scores ?? {};

  const sendSocketEvent = useCallback(
    (event: Record<string, unknown>): boolean => {
      if (socketRef.current && socketRef.current.readyState === WebSocket.OPEN) {
        socketRef.current.send(JSON.stringify(event));
        return true;
      }
      setError("You are not connected to the table. Reconnect before taking an action.");
      return false;
    },
    [],
  );

  const connectSocket = useCallback((roomId: string) => {
    setConnectionStatus("connecting");
    socketErrorRef.current = null;
    if (socketRef.current) {
      socketRef.current.close();
    }

    const sameOriginWsBase = `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/api`;
    const socket = new WebSocket(`${WS_BASE || sameOriginWsBase}/ws/rooms/${roomId}`);
    socketRef.current = socket;

    socket.onopen = () => {
      if (socketRef.current === socket) {
        setConnectionStatus("connected");
      }
    };

    socket.onmessage = (event) => {
      const payload = JSON.parse(event.data) as {
        type?: string;
        code?: string;
        room?: Room;
        message?: string;
      };

      if (payload.type === "room_state" && payload.room) {
        if (socketErrorRef.current === null) {
          setError("");
        }
        setVotingInFlight(false);
        setRoom(payload.room);
        return;
      }

      if (payload.type === "error") {
        setVotingInFlight(false);
        const message = payload.message ?? "Unable to complete that action.";
        if (payload.code === "outcome_uncertain" || payload.code === "storage_unavailable") {
          socketErrorRef.current = message;
        }
        setError(message);
      }
    };

    socket.onclose = (event) => {
      if (socketRef.current !== socket) {
        return;
      }
      socketRef.current = null;

      const storedSession = readStoredSession();
      if (storedSession && storedSession.room_id === roomId) {
        setConnectionStatus(
          event.code === 1008 || event.code === 1006 ? "rejected" : "disconnected",
        );
        setError(socketCloseMessage(event.code, socketErrorRef.current));
      }
    };
  }, []);

  const restoreSession = useCallback(async () => {
    if (restoreAttemptedRef.current) {
      return;
    }

    const storedSession = readStoredSession();
    if (!storedSession) {
      return;
    }

    restoreAttemptedRef.current = true;
    setError("");
    setRestoreUnavailable(false);
    setIsBusy(true);

    const result = await restoreRoomSession(storedSession, API_BASE);
    if (result.kind === "restored") {
      setActivePlayerId(result.player.id);
      setRoom(result.room);
      setJoinRoomId(result.room.room_id);
      setJoinName(result.player.name);
      connectSocket(result.room.room_id);
      setIsBusy(false);
      return;
    }

    if (result.kind === "unauthorized" || result.kind === "not-found" || result.kind === "player-mismatch") {
      clearStoredSession();
      setJoinRoomId(storedSession.room_id);
      setJoinName(storedSession.player_name);
      setError(
        result.kind === "unauthorized"
          ? "Your saved session expired. Please join again."
          : result.kind === "not-found"
            ? "That saved room no longer exists. Please join another room."
            : "Your saved player was not found in that room. Please join again.",
      );
      setIsBusy(false);
      return;
    }

    setRestoreUnavailable(true);
    setError("Could not restore your saved session right now. Your saved session was kept; try again.");
    setIsBusy(false);
  }, [connectSocket]);

  const retryRestore = () => {
    restoreAttemptedRef.current = false;
    void restoreSession();
  };

  const reconnectSocket = () => {
    if (!room || !activePlayerId || connectionStatus === "connecting") {
      return;
    }
    setError("");
    connectSocket(room.room_id);
  };

  useEffect(() => {
    const timeoutId = window.setTimeout(() => {
      void restoreSession();
    }, 0);

    return () => {
      window.clearTimeout(timeoutId);
    };
  }, [restoreSession]);

  const handleCreateRoom = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError("");
    setIsBusy(true);
    const requestedName = roomName.trim();

    try {
      if (!createRequestRef.current) {
        createRequestRef.current = readRoomCreateAttempt();
      }
      if (createRequestRef.current?.name !== requestedName) {
        createRequestRef.current = { name: requestedName, key: crypto.randomUUID() };
      }
      window.localStorage.setItem(
        ROOM_CREATE_ATTEMPT_KEY,
        JSON.stringify(createRequestRef.current),
      );
      const nextRoom = await createRoom(requestedName, createRequestRef.current.key);
      createRequestRef.current = null;
      window.localStorage.removeItem(ROOM_CREATE_ATTEMPT_KEY);
      setRoom(nextRoom);
      setJoinRoomId(nextRoom.room_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to create a room.");
    } finally {
      setIsBusy(false);
    }
  };

  const handleJoinRoom = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmedRoomId = joinRoomId.trim();
    const trimmedName = joinName.trim();

    if (!trimmedRoomId || !trimmedName) {
      setError("Choose a room code and player name.");
      return;
    }

    setError("");
    setIsBusy(true);

    try {
      if (!joinRequestRef.current) {
        joinRequestRef.current = readRoomJoinAttempt();
      }
      if (
        joinRequestRef.current?.roomId !== trimmedRoomId ||
        joinRequestRef.current.name !== trimmedName
      ) {
        joinRequestRef.current = {
          roomId: trimmedRoomId,
          name: trimmedName,
          key: await createJoinIdempotencyKey(trimmedRoomId, trimmedName),
        };
      }
      window.localStorage.setItem(
        ROOM_JOIN_ATTEMPT_KEY,
        JSON.stringify(joinRequestRef.current),
      );
      const payload = await joinRoom(
        trimmedRoomId,
        trimmedName,
        joinRequestRef.current.key,
        API_BASE,
      );
      joinRequestRef.current = null;
      window.localStorage.removeItem(ROOM_JOIN_ATTEMPT_KEY);
      writeStoredSession(payload.room_id, payload.player.name);
      const nextRoom = await readRoom(payload.room_id);
      setActivePlayerId(payload.player.id);
      setRoom(nextRoom);
      setJoinRoomId(payload.room_id);
      setJoinName(payload.player.name);
      connectSocket(payload.room_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to join room.");
    } finally {
      setIsBusy(false);
    }
  };

  const toggleReady = () => {
    if (!room || !activePlayerId) {
      return;
    }

    const nextReady = !myPlayer?.ready;
    sendSocketEvent({ type: "toggle_ready", ready: nextReady });
  };

  const startGame = () => {
    if (!room || room.players.length < 2) {
      setError("At least 2 players are required to start.");
      return;
    }

    if (sendSocketEvent({ type: "start_game" })) {
      setError("");
    }
  };

  const voteOnRound = (belief: boolean) => {
    if (
      !room ||
      !activePlayerId ||
      !room.game ||
      activePlayerId === room.game.speaker_id ||
      votingInFlight ||
      room.game.has_voted ||
      room.game.resolved
    ) {
      return;
    }

    if (sendSocketEvent({ type: "vote", belief })) {
      setVotingInFlight(true);
      setError("");
    }
  };

  const nextRound = () => {
    if (!room) {
      return;
    }

    sendSocketEvent({ type: "start_game" });
  };

  return (
    <main className="page-shell">
      <section className="game-card" aria-labelledby="game-title">
        <p className="eyebrow">A real-time party game for 2–8 players</p>
        <h1 id="game-title">
          Bluff <span>or</span> Believe
        </h1>
        <p className="subtitle">The Big Fib</p>

        <div className="lobby-grid">
          <form className="panel" onSubmit={handleCreateRoom}>
            <h2>Create a table</h2>
            <label className="field">
              <span>Table name</span>
              <input
                value={roomName}
                onChange={(event) => setRoomName(event.target.value)}
                placeholder="Moonlit Manor"
              />
            </label>
            <button type="submit" disabled={isBusy || roomName.trim().length === 0}>
              {isBusy ? "Creating..." : "Create room"}
            </button>
          </form>

          <form className="panel" onSubmit={handleJoinRoom}>
            <h2>Join a table</h2>
            <label className="field">
              <span>Room code</span>
              <input
                value={joinRoomId}
                onChange={(event) => setJoinRoomId(event.target.value)}
                placeholder="Paste room code"
              />
            </label>
            <label className="field">
              <span>Your name</span>
              <input
                value={joinName}
                onChange={(event) => setJoinName(event.target.value)}
                placeholder="Player One"
              />
            </label>
            <button
              type="submit"
              disabled={isBusy || joinRoomId.trim().length === 0 || joinName.trim().length === 0}
            >
              Join room
            </button>
          </form>
        </div>

        {error ? <p className="error-banner">{error}</p> : null}
        {connectionStatus === "connecting" ? (
          <p role="status">Connecting to your table...</p>
        ) : null}
        {connectionStatus === "connected" ? <p role="status">Connected to your table.</p> : null}
        {restoreUnavailable ? (
          <button type="button" onClick={retryRestore} disabled={isBusy}>
            Retry session restore
          </button>
        ) : null}

        {room ? (
          <div className="room-panel">
            {activePlayerId ? (
              <button
                type="button"
                className="ghost"
                onClick={reconnectSocket}
                disabled={connectionStatus === "connecting"}
              >
                {connectionStatus === "connecting"
                  ? "Connecting..."
                  : "Refresh / reconnect"}
              </button>
            ) : null}
            <div className="room-header">
              <div>
                <p className="room-label">Current table</p>
                <h3>{room.name}</h3>
              </div>
              <span className="room-code">#{room.room_id}</span>
            </div>

            <div className="players-list" aria-live="polite">
              {room.players.map((player) => (
                <div
                  key={player.id}
                  className={`player-card ${player.id === activePlayerId ? "active" : ""}`}
                >
                  <div>
                    <strong>{player.name}</strong>
                    <small>{player.ready ? "Ready" : "Waiting"}</small>
                  </div>
                  <span className={player.ready ? "badge ready" : "badge"}>
                    {player.ready ? "Ready" : "Not ready"}
                  </span>
                </div>
              ))}
            </div>

            {myPlayer ? (
              <div className="room-actions">
                <button
                  type="button"
                  className={myPlayer.ready ? "secondary" : ""}
                  onClick={toggleReady}
                >
                  {myPlayer.ready ? "Mark unready" : "Mark ready"}
                </button>
                <button type="button" className="ghost" onClick={startGame} disabled={room.players.length < 2 || isBusy}>
                  Start game
                </button>
              </div>
            ) : null}

            {room.game ? (
              <div className="game-screen">
                <div className="game-header">
                  <p className="room-label">Round {room.game.round_number}</p>
                  <h4>{room.game.prompt}</h4>
                </div>

                <div className="statement-box">
                  <p>{room.game.statement}</p>
                  {room.game.resolved ? (
                    <strong>
                      {room.game.truth_value
                        ? "The story was true."
                        : "The story was a bluff."}
                    </strong>
                  ) : (
                    <strong>
                      {currentSpeaker ? `${currentSpeaker.name} is speaking.` : "Speaker is taking a turn."}
                    </strong>
                  )}
                </div>

                {activePlayerId && activePlayerId !== room.game.speaker_id && !room.game.resolved ? (
                  <div className="vote-actions">
                    <button
                      type="button"
                      onClick={() => void voteOnRound(true)}
                      disabled={votingInFlight || room.game.has_voted}
                    >
                      Believe
                    </button>
                    <button
                      type="button"
                      className="ghost"
                      onClick={() => void voteOnRound(false)}
                      disabled={votingInFlight || room.game.has_voted}
                    >
                      Call bluff
                    </button>
                  </div>
                ) : null}

                {room.game.resolved && activePlayerId === room.game.speaker_id ? (
                  <div className="round-footer">
                    <button type="button" onClick={() => void nextRound()}>
                      Next round
                    </button>
                  </div>
                ) : null}

                {room.game.resolved ? (
                  <div className="vote-summary">
                    {room.players
                      .filter((player) => player.id !== room.game?.speaker_id)
                      .map((player) => (
                        <div key={player.id} className="vote-item">
                          <span>{player.name}</span>
                          <strong>
                            {room.game?.votes?.[player.id] === undefined
                              ? "No vote"
                              : room.game.votes?.[player.id]
                                ? "Believe"
                                : "Bluff"}
                          </strong>
                        </div>
                      ))}
                  </div>
                ) : null}
              </div>
            ) : null}

            <div className="score-panel">
              <h4>Scoreboard</h4>
              <div className="score-grid">
                {room.players.map((player) => (
                  <div key={player.id} className="score-card">
                    <span>{player.name}</span>
                    <strong>{scoreMap[player.id] ?? 0}</strong>
                  </div>
                ))}
              </div>
            </div>
          </div>
        ) : (
          <div className="status-panel">
            <span className="status-dot" aria-hidden="true" />
            <span>Create or join a room to begin the lobby.</span>
          </div>
        )}
      </section>
    </main>
  );
}
