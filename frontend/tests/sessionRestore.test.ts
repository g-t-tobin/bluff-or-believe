import assert from "node:assert/strict";
import test from "node:test";

import {
  createJoinIdempotencyKey,
  joinRoom,
  restoreRoomSession,
  socketCloseMessage,
} from "../src/sessionRestore.ts";

const session = { room_id: "room-123", player_name: "avery" };
const room = {
  room_id: "room-123",
  name: "Table",
  players: [
    { id: "player-1", name: "Avery", ready: false },
    { id: "player-2", name: "Blake", ready: true },
  ],
};

function response(status: number, body: unknown = room): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

test("restores the saved player with a credentialed GET and no join POST", async () => {
  const requests: { input: string; init?: RequestInit }[] = [];
  const result = await restoreRoomSession(session, "/api", async (input, init) => {
    requests.push({ input: String(input), init });
    return response(200);
  });

  assert.equal(result.kind, "restored");
  if (result.kind === "restored") {
    assert.equal(result.player.id, "player-1");
    assert.equal(result.room.room_id, "room-123");
  }
  assert.deepEqual(requests, [
    { input: "/api/rooms/room-123", init: { credentials: "include" } },
  ]);
});

test("classifies unauthorized and missing rooms as stale sessions", async () => {
  assert.deepEqual(
    await restoreRoomSession(session, "/api", async () => response(401)),
    { kind: "unauthorized" },
  );
  assert.deepEqual(
    await restoreRoomSession(session, "/api", async () => response(404)),
    { kind: "not-found" },
  );
});

test("retains a saved session after network and server failures", async () => {
  assert.deepEqual(
    await restoreRoomSession(session, "/api", async () => {
      throw new TypeError("network unavailable");
    }),
    { kind: "transient-error" },
  );
  assert.deepEqual(
    await restoreRoomSession(session, "/api", async () => response(503)),
    { kind: "transient-error" },
  );
});

test("returns a mismatch when the authenticated room lacks the saved player name", async () => {
  const result = await restoreRoomSession(session, "/api", async () =>
    response(200, {
      ...room,
      players: [{ id: "player-2", name: "Blake", ready: true }],
    }),
  );
  assert.deepEqual(result, { kind: "player-mismatch" });
});

test("explicit joins still POST with credentials included", async () => {
  let requestUrl = "";
  let requestInit: RequestInit | undefined;
  const key = "j1.nonce.bound-digest";
  const result = await joinRoom("room-123", "Avery", key, "/api", async (input, init) => {
    requestUrl = String(input);
    requestInit = init;
    return response(201, {
      room_id: "room-123",
      player: room.players[0],
    });
  });

  assert.equal(requestUrl, "/api/rooms/room-123/players");
  assert.equal(requestInit?.method, "POST");
  assert.equal(requestInit?.credentials, "include");
  assert.equal(
    new Headers(requestInit?.headers).get("Idempotency-Key"),
    key,
  );
  assert.deepEqual(JSON.parse(String(requestInit?.body)), { name: "Avery" });
  assert.equal(result.player.id, "player-1");
});

test("join idempotency keys are bound to the exact room and player name", async () => {
  const key = await createJoinIdempotencyKey("room-123", "Avery");
  assert.match(key, /^j1\.[0-9a-f]{64}\.[0-9a-f]{64}$/);

  const differentNameKey = await createJoinIdempotencyKey("room-123", "Blake");
  const differentRoomKey = await createJoinIdempotencyKey("room-456", "Avery");
  assert.notEqual(key, differentNameKey);
  assert.notEqual(key, differentRoomKey);
});

test("close code 1013 preserves explicit storage and uncertain-outcome messages", () => {
  const uncertain = "Action outcome is uncertain. Reconnect before retrying.";
  assert.equal(socketCloseMessage(1013, uncertain), uncertain);
  assert.match(socketCloseMessage(1013, null), /Refresh or reconnect/);
});
