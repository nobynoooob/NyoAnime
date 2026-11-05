import { authenticate } from "../_shared/auth.ts";
import {
  corsHeaders,
  failure,
  failureForCode,
  isValidUuid,
  json,
  readJson,
  rpcFailure,
} from "../_shared/http.ts";

const NICKNAME_MAX = 32;

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  const caller = await authenticate(req);
  if (caller instanceof Response) return caller;

  const body = await readJson(req);
  if (body instanceof Response) return body;

  // Identity is taken exclusively from the verified JWT. A user_id supplied in
  // the body is ignored so guests cannot impersonate another account.
  const roomCode = typeof body.room_code === "string" ? body.room_code.trim() : "";
  const nickname = typeof body.nickname === "string" ? body.nickname.trim() : "";

  if (!/^[0-9]{6}$/.test(roomCode)) {
    return failure("invalid_room_code", "Room code must be six digits", 400);
  }
  if (nickname.length < 1 || nickname.length > NICKNAME_MAX) {
    return failure("invalid_nickname", "Nickname must be 1-32 characters", 400);
  }

  const { data, error } = await caller.client.rpc("join_watch_room", {
    p_room_code: roomCode,
    p_nickname: nickname,
  });

  if (error) return rpcFailure(error);

  const room = Array.isArray(data) ? data[0] : data;
  if (room?.error_code) {
    return failureForCode(String(room.error_code), `Join rejected: ${room.error_code}`);
  }
  if (!room?.room_id || !room?.participant_id) {
    return failure("join_failed", "Room join did not return a membership", 500);
  }
  if (!isValidUuid(room.room_id) || !isValidUuid(room.participant_id)) {
    return failure("join_failed", "Room response was malformed", 500);
  }

  return json({
    success: true,
    room_id: room.room_id,
    room_code: room.room_code ?? roomCode,
    participant_id: room.participant_id,
    role: room.role ?? "guest",
    nickname: room.nickname ?? nickname,
  });
});
