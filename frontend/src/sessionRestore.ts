export type SessionPlayer = {
  id: string;
  name: string;
  ready: boolean;
};

export type SessionRoom = {
  room_id: string;
  name: string;
  players: SessionPlayer[];
  scores?: Record<string, number>;
  game?: {
    round_number: number;
    speaker_id: string;
    prompt: string;
    statement: string;
    truth_value?: boolean;
    votes?: Record<string, boolean>;
    has_voted?: boolean;
    resolved: boolean;
  };
};

export type StoredRoomSession = {
  room_id: string;
  player_name: string;
};

export async function createJoinIdempotencyKey(
  roomId: string,
  playerName: string,
  cryptoProvider: Crypto = crypto,
): Promise<string> {
  const nonceBytes = new Uint8Array(32);
  cryptoProvider.getRandomValues(nonceBytes);
  const nonce = Array.from(nonceBytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  const binding = new TextEncoder().encode(
    `join-v1\u0000${roomId}\u0000${playerName.trim()}\u0000${nonce}`,
  );
  const digest = new Uint8Array(await cryptoProvider.subtle.digest("SHA-256", binding));
  const digestHex = Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `j1.${nonce}.${digestHex}`;
}

export type RestoreResult =
  | { kind: "restored"; room: SessionRoom; player: SessionPlayer }
  | { kind: "unauthorized" | "not-found" | "player-mismatch" }
  | { kind: "transient-error" };

export function socketCloseMessage(code: number, serverMessage: string | null): string {
  if (code === 1013 && serverMessage) {
    return serverMessage;
  }
  if (code === 1013) {
    return "Room storage is temporarily unavailable. Refresh or reconnect before retrying.";
  }
  if (code === 1008 || code === 1006) {
    return "Connection was rejected or unavailable. Check your session and try reconnecting.";
  }
  return "Connection lost. Use Refresh / reconnect to return to your table.";
}

export async function restoreRoomSession(
  session: StoredRoomSession,
  apiBase: string,
  fetcher: typeof fetch = fetch,
): Promise<RestoreResult> {
  let response: Response;
  try {
    response = await fetcher(`${apiBase}/rooms/${encodeURIComponent(session.room_id)}`, {
      credentials: "include",
    });
  } catch {
    return { kind: "transient-error" };
  }

  if (response.status === 401) {
    return { kind: "unauthorized" };
  }
  if (response.status === 404) {
    return { kind: "not-found" };
  }
  if (!response.ok) {
    return { kind: "transient-error" };
  }

  let room: SessionRoom;
  try {
    room = (await response.json()) as SessionRoom;
  } catch {
    return { kind: "transient-error" };
  }

  if (!room || !Array.isArray(room.players)) {
    return { kind: "transient-error" };
  }

  const player = room.players.find(
    (candidate) =>
      candidate != null &&
      typeof candidate.name === "string" &&
      candidate.name.trim().toLowerCase() === session.player_name.trim().toLowerCase(),
  );
  if (!player) {
    return { kind: "player-mismatch" };
  }

  return { kind: "restored", room, player };
}

export async function joinRoom(
  roomId: string,
  name: string,
  idempotencyKey: string,
  apiBase: string,
  fetcher: typeof fetch = fetch,
): Promise<{ player: SessionPlayer; room_id: string }> {
  const response = await fetcher(`${apiBase}/rooms/${encodeURIComponent(roomId)}/players`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify({ name }),
  });

  if (!response.ok) {
    const payload = (await response.json().catch(() => ({ detail: "Unable to join room." }))) as {
      detail?: string;
    };
    throw new Error(payload.detail ?? "Unable to join room.");
  }

  return response.json() as Promise<{ player: SessionPlayer; room_id: string }>;
}
