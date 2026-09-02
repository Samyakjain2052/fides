// ============================================================================
// REAL authentication against the DataShield backend.
//
// This file is not mock data. Login, signup, refresh and logout hit
// http://localhost:8100/v1/auth/* and create genuine rows in PostgreSQL.
// The rest of the product still runs on the mocks in ./index.js until the
// backend's consent and DSAR phases land — see frontend/README.md.
//
// Two rules that shape everything here:
//
// 1. The refresh token is an HttpOnly cookie. JavaScript cannot read it, which
//    is the point: an XSS payload cannot steal the session. So every auth call
//    sends `credentials: "include"`, and we never touch the cookie ourselves.
// 2. The ACCESS token is kept in memory only — deliberately not localStorage.
//    Anything JavaScript can read, injected JavaScript can read. Losing it on a
//    page reload is fine, because /auth/refresh silently restores the session
//    from the cookie.
// ============================================================================

// Same-origin by default, in dev as well as production.
//
// This used to default to http://localhost:8100/v1 — a different origin from
// the app. The backend's refresh cookie is SameSite=Strict, so a cross-origin
// deployment would have had the browser silently withhold it: sign-in succeeds,
// every reload signs you out, and nothing appears in any log. Dev proxies /api
// to the backend (vite.config.js) and nginx proxies it in production, so both
// environments exercise the same path and that failure cannot be prod-only.
const API = import.meta.env.VITE_API_URL || "/api/v1";

// In memory, not localStorage. See rule 2 above.
let accessToken = null;
let currentUser = null;
let capabilities = [];

export function getAccessToken() {
  return accessToken;
}

export function getCurrentUser() {
  return currentUser;
}

export function getCapabilities() {
  return capabilities;
}

export function clearSession() {
  accessToken = null;
  currentUser = null;
  capabilities = [];
}

function applySession(data) {
  accessToken = data.access_token;
  currentUser = data.user;
  capabilities = data.capabilities || [];
  return data;
}

/** Turn the backend's RFC 7807 problem+json into a message worth showing. */
async function toError(resp) {
  let body = null;
  try {
    body = await resp.json();
  } catch {
    /* empty body */
  }
  if (body?.errors?.length) {
    // Field-level validation: surface the first specific complaint rather than
    // a useless "Validation failed".
    const first = body.errors[0];
    return new Error(`${first.field}: ${first.problem}`);
  }
  const message =
    typeof body?.detail === "string"
      ? body.detail
      : body?.title || `Request failed (${resp.status})`;
  const err = new Error(message);
  err.status = resp.status;
  err.requestId = body?.request_id;
  return err;
}

async function call(path, { method = "GET", body, auth = false } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (auth && accessToken) headers.Authorization = `Bearer ${accessToken}`;

  const resp = await fetch(`${API}${path}`, {
    method,
    headers,
    // Required for the HttpOnly refresh cookie to be sent and set. The backend
    // allows this origin explicitly; a wildcard CORS origin with credentials is
    // rejected by the browser, which is why the API pins the origin.
    credentials: "include",
    body: body ? JSON.stringify(body) : undefined,
  });

  if (!resp.ok) throw await toError(resp);
  return resp.status === 204 ? null : resp.json();
}

// ---------------------------------------------------------------- public API --

/** Create an organisation and its first Admin/DPO, and sign them straight in. */
export async function register({ companyName, workspace, adminName, adminEmail, password }) {
  return applySession(
    await call("/auth/register", {
      method: "POST",
      body: {
        company_name: companyName,
        workspace: workspace || undefined,
        admin_name: adminName,
        admin_email: adminEmail,
        password,
      },
    })
  );
}

export async function login({ workspace, email, password }) {
  return applySession(
    await call("/auth/login", {
      method: "POST",
      body: { tenant_slug: workspace, email, password },
    })
  );
}

/**
 * Turn an invitation into an account, and sign straight in.
 *
 * Alongside `register` and `login` on purpose: it creates a session, so it needs
 * the same `credentials: "include"` and the same `applySession` path. A separate
 * implementation elsewhere would be exactly where the refresh cookie quietly
 * stopped being set.
 *
 * The person accepting has no session yet, so this is unauthenticated — `call`
 * without `auth: true`.
 */
export async function acceptInvitation({ token, fullName, password }) {
  return applySession(
    await call("/auth/accept-invitation", {
      method: "POST",
      body: { token, full_name: fullName, password },
    })
  );
}

/**
 * Restore a session from the HttpOnly cookie.
 *
 * Called on page load. Failure is normal and not an error — it just means
 * nobody is signed in — so this resolves to null rather than throwing.
 */
export async function restoreSession() {
  try {
    return applySession(await call("/auth/refresh", { method: "POST" }));
  } catch {
    clearSession();
    return null;
  }
}

/**
 * Authenticated call to any v1 endpoint, with one automatic retry after a
 * token refresh.
 *
 * The access token lives 15 minutes and only in memory. Without this retry,
 * every screen left open for a quarter of an hour would start failing with a
 * 401 that a page reload silently fixes — the kind of bug that gets reported as
 * "it logs me out randomly" and is miserable to reproduce.
 *
 * The retry happens exactly once. If the refresh itself fails the session is
 * genuinely over, and looping would just turn that into a hang.
 */
export async function apiFetch(path, options = {}) {
  try {
    return await call(path, { ...options, auth: true });
  } catch (err) {
    if (err.status !== 401) throw err;
    const session = await restoreSession();
    if (!session) throw err;
    return call(path, { ...options, auth: true });
  }
}

/**
 * Authenticated download of a streamed export.
 *
 * Separate from `apiFetch` because `call()` parses every response as JSON, and a
 * CSV is not JSON. Returns the Blob and the server's filename so the caller can
 * hand it to the browser.
 *
 * Same one-shot refresh-and-retry as `apiFetch`: a report generated on a screen
 * left open past the access token's 15 minutes must not fail with a 401 that a
 * reload would have fixed.
 */
export async function apiDownload(path, { method = "POST", body } = {}) {
  const attempt = async () => {
    const headers = {};
    if (body) headers["Content-Type"] = "application/json";
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
    const resp = await fetch(`${API}${path}`, {
      method,
      headers,
      credentials: "include",
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!resp.ok) throw await toError(resp);

    // The server names the file. Deriving it in the browser would drift from the
    // period the server actually used once a default is involved.
    const disposition = resp.headers.get("content-disposition") || "";
    const match = /filename="?([^"';]+)"?/.exec(disposition);
    return { blob: await resp.blob(), filename: match?.[1] || "report" };
  };

  try {
    return await attempt();
  } catch (err) {
    if (err.status !== 401) throw err;
    const session = await restoreSession();
    if (!session) throw err;
    return attempt();
  }
}

export async function logout() {
  try {
    await call("/auth/logout", { method: "POST", auth: true });
  } finally {
    // Clear locally even if the server call failed — the user asked to leave.
    clearSession();
  }
}

/** Live availability check for the signup form. */
export async function checkWorkspace(workspace) {
  if (!workspace || workspace.length < 2) return { available: false, reason: null };
  return call(`/auth/workspace-available?workspace=${encodeURIComponent(workspace)}`);
}

/** Is the backend reachable? The UI says so plainly rather than failing oddly. */
export async function backendHealthy() {
  try {
    const resp = await fetch(`${API.replace(/\/v1$/, "")}/health`, { method: "GET" });
    return resp.ok;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------- helpers ----

/**
 * Mirrors the server's password policy so the form can give feedback as you
 * type. The SERVER is the authority — this is a courtesy, and it is why the
 * same rules exist in registration_service.py.
 */
export function passwordProblems(password, { email = "", name = "" } = {}) {
  const problems = [];
  if (password.length < 12) problems.push("At least 12 characters");
  if (new Set(password).size < 5) problems.push("Too repetitive");
  const lowered = password.toLowerCase();
  if (email && lowered.includes(email.toLowerCase())) problems.push("Contains your email");
  const local = email.split("@")[0]?.toLowerCase();
  if (local && local.length >= 4 && lowered.includes(local)) problems.push("Contains your email");
  if (name && name.length >= 4 && lowered.includes(name.toLowerCase()))
    problems.push("Contains your name");
  return problems;
}

/** Suggest a workspace id from a company name — same rules as the server. */
export function suggestWorkspace(companyName) {
  const map = {
    ø: "o", æ: "ae", œ: "oe", ð: "d", þ: "th", ß: "ss", ł: "l", đ: "d",
  };
  return companyName
    .toLowerCase()
    .replace(/['']/g, "")
    .replace(/[øæœðþßłđ]/g, (c) => map[c] || c)
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 63)
    .replace(/-+$/, "");
}

/**
 * Users in this workspace — needed to assign a grievance to somebody.
 *
 * Lives here rather than in a `users.js` because module 08 (users & roles) is
 * still preview: this is the one read the rest of the product genuinely needs
 * today, and inventing a client for an unbuilt module would be worse.
 *
 * Requires USER_MANAGE. A grievance_officer will get a 403 — deliberately, since
 * the whole point of that role is that it cannot read anything but grievances —
 * so callers must handle the rejection rather than treat it as an error.
 */
export function listUsers() {
  return apiFetch("/admin/users");
}


/**
 * Ask for a password reset link.
 *
 * Resolves the same way whether or not that address has an account — the server
 * refuses to reveal it, because for a DPDP product "does this person have an
 * account with this company" is itself personal data. So the UI must not promise
 * an email arrived; it can only say one was sent if the address is known.
 *
 * Replaces `sendResetLink` in api/index.js, which waited 500ms and returned
 * success without making a network call.
 */
export function requestPasswordReset({ workspace, email }) {
  return call("/auth/forgot-password", {
    method: "POST",
    body: { workspace: workspace.trim().toLowerCase(), email: email.trim() },
  });
}

/** Set a new password from a reset link, and get signed in. */
export async function resetPassword({ token, password }) {
  return applySession(
    await call("/auth/reset-password", {
      method: "POST",
      body: { token, password },
    }),
  );
}
