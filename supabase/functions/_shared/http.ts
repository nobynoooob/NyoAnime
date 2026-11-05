export const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}

export function failure(code: string, message: string, status = 400): Response {
  return json({ success: false, error: { code, message } }, status);
}

// HTTP status mapping for the error codes raised or returned by the RPCs.
const ERROR_STATUS: Record<string, number> = {
  not_authenticated: 401,
  invalid_nickname: 400,
  invalid_language_mode: 400,
  invalid_capacity: 400,
  invalid_room_code: 400,
  invalid_room_id: 400,
  not_a_member: 403,
  room_not_found: 404,
  room_expired: 410,
  room_full: 409,
  too_many_rooms: 429,
  rate_limited: 429,
  room_code_generation_failed: 500,
  room_code_secret_missing: 503,
};

export function failureForCode(code: string, message = code): Response {
  return failure(code, message, ERROR_STATUS[code] ?? 400);
}

// Maps the SQL error codes raised by the Watch Together RPCs to HTTP responses.
export function rpcFailure(error: { message?: string; code?: string } | null): Response {
  const message = error?.message ?? "internal_error";
  for (const code of Object.keys(ERROR_STATUS)) {
    if (message.includes(code)) return failureForCode(code, message);
  }
  return failure("internal_error", message, 500);
}

export async function readJson(req: Request): Promise<Record<string, unknown> | Response> {
  try {
    const body = await req.json();
    if (!body || typeof body !== "object" || Array.isArray(body)) {
      return failure("invalid_body", "Expected a JSON object", 400);
    }
    return body as Record<string, unknown>;
  } catch {
    return failure("invalid_json", "Malformed JSON body", 400);
  }
}

export function isValidUuid(value: unknown): value is string {
  return typeof value === "string" &&
    /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(value);
}
