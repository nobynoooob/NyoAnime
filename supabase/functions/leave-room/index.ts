import { authenticate } from "../_shared/auth.ts";
import {
  corsHeaders,
  failure,
  isValidUuid,
  json,
  readJson,
  rpcFailure,
} from "../_shared/http.ts";

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  const caller = await authenticate(req);
  if (caller instanceof Response) return caller;

  const body = await readJson(req);
  if (body instanceof Response) return body;

  const roomId = typeof body.room_id === "string" ? body.room_id.trim() : "";
  if (!isValidUuid(roomId)) {
    return failure("invalid_room_id", "room_id must be a UUID", 400);
  }

  // The RPC derives the caller from auth.uid() and verifies membership before
  // deleting anything. A non-member receives false and is rejected here.
  const { data, error } = await caller.client.rpc("leave_watch_room", {
    p_room_id: roomId,
  });

  if (error) return rpcFailure(error);
  if (data !== true) {
    return failure("not_a_member", "You are not a member of this room", 403);
  }

  return json({ success: true, room_id: roomId });
});
