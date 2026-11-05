import { createClient, type SupabaseClient, type User } from "npm:@supabase/supabase-js@2";
import { failure, corsHeaders } from "./http.ts";

export type AuthenticatedCaller = {
  client: SupabaseClient;
  user: User;
};

/**
 * Builds a user-scoped Supabase client from the caller's bearer token and
 * verifies that token with GoTrue. The returned client carries the caller's
 * JWT, so auth.uid() inside the RPCs is the verified user and RLS applies.
 *
 * Only the public anon key is used. The service-role key is never needed by
 * these endpoints and is never returned to clients.
 */
export async function authenticate(
  req: Request,
): Promise<AuthenticatedCaller | Response> {
  if (req.method !== "POST") {
    return new Response("Method not allowed", {
      status: 405,
      headers: corsHeaders,
    });
  }

  const authorization = req.headers.get("Authorization");
  if (!authorization?.startsWith("Bearer ")) {
    return failure("missing_authorization", "A bearer token is required", 401);
  }
  const token = authorization.slice("Bearer ".length).trim();
  if (!token) {
    return failure("missing_authorization", "A bearer token is required", 401);
  }

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const anonKey = Deno.env.get("SUPABASE_ANON_KEY") ??
    Deno.env.get("SUPABASE_PUBLISHABLE_KEY");
  if (!supabaseUrl || !anonKey) {
    return failure("server_misconfigured", "Supabase environment is incomplete", 500);
  }

  const client = createClient(supabaseUrl, anonKey, {
    auth: {
      persistSession: false,
      autoRefreshToken: false,
      detectSessionInUrl: false,
    },
    global: { headers: { Authorization: authorization } },
  });

  // Pass the token explicitly so verification never depends on cached state.
  const { data, error } = await client.auth.getUser(token);
  if (error || !data?.user) {
    return failure("invalid_token", "The bearer token is invalid or expired", 401);
  }

  return { client, user: data.user };
}
