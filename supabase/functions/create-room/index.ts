import { authenticate } from "../_shared/auth.ts";
import {
  corsHeaders,
  failure,
  isValidUuid,
  json,
  readJson,
  rpcFailure,
} from "../_shared/http.ts";

const NICKNAME_MAX = 32;
const ALLOWED_MODES = new Set(["ar_sub", "en_sub", "en_dub"]);

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  const caller = await authenticate(req);
  if (caller instanceof Response) return caller;

  const body = await readJson(req);
  if (body instanceof Response) return body;

  // Identity is taken exclusively from the verified JWT. Any user_id or
  // host_user_id in the request body is deliberately ignored.
  const nickname = typeof body.nickname === "string" ? body.nickname.trim() : "";
  const languageMode = typeof body.language_mode === "string"
    ? body.language_mode
    : "ar_sub";
  const maximumMembers = body.maximum_members === undefined
    ? 8
    : Number(body.maximum_members);

  if (nickname.length < 1 || nickname.length > NICKNAME_MAX) {
    return failure("invalid_nickname", "Nickname must be 1-32 characters", 400);
  }
  if (!ALLOWED_MODES.has(languageMode)) {
    return failure("invalid_language_mode", "Unsupported language mode", 400);
  }
  if (!Number.isInteger(maximumMembers) || maximumMembers < 2 || maximumMembers > 8) {
    return failure("invalid_capacity", "maximum_members must be 2-8", 400);
  }

  const { data, error } = await caller.client.rpc("create_watch_room", {
    p_nickname: nickname,
    p_language_mode: languageMode,
    p_max_members: maximumMembers,
  });

  if (error) return rpcFailure(error);

  const room = Array.isArray(data) ? data[0] : data;
  if (!room?.room_id || !room?.room_code || !room?.participant_id) {
    return failure("room_create_failed", "Room was not created", 500);
  }
  if (!isValidUuid(room.room_id) || !isValidUuid(room.participant_id)) {
    return failure("room_create_failed", "Room response was malformed", 500);
  }

  return json({
    success: true,
    room_id: room.room_id,
    room_code: room.room_code,
    participant_id: room.participant_id,
    role: room.role ?? "host",
    expires_at: room.expires_at,
  });
});
