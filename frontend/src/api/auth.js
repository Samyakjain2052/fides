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

const API = import.meta.env.VITE_API_URL || "http://localhost:8100/v1";

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
