// ============================================================================
// The unauthenticated rights endpoints.
//
// Deliberately does NOT go through `apiFetch`. That helper attaches a bearer
// token, sends credentials, and retries through a session refresh — all of
// which are wrong here and one of which is actively harmful: these endpoints
// answer with `Access-Control-Allow-Origin: *`, and a browser refuses that
// alongside credentials. Sending them would break the form on every customer's
// domain, which is the only place it runs.
//
// That refusal is also what makes these endpoints CSRF-safe by construction:
// no session can travel to them, so nothing reachable here can act on a
// signed-in user.
// ============================================================================

// Same base the rest of the client uses, so a deployment behind a path prefix
// or a different host needs no second setting.
const API = import.meta.env.VITE_API_BASE_URL || "";

async function call(path, { method = "GET", body } = {}) {
  const resp = await fetch(`${API}${path}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    // Explicitly omitted, not merely left default: see the header.
    credentials: "omit",
    body: body ? JSON.stringify(body) : undefined,
  });

  let payload = null;
  try {
    payload = await resp.json();
  } catch {
    /* a non-JSON error page; fall through to the status-based message */
  }

  if (!resp.ok) {
    const message =
      payload?.detail ||
      payload?.title ||
      (resp.status === 429
        ? "Too many requests just now. Please try again shortly."
        : "Something went wrong. Please try again.");
    const err = new Error(message);
    err.status = resp.status;
    throw err;
  }
  return payload;
}

/**
 * What the form should offer, and the deadline it may honestly quote.
 *
 * Served rather than hardcoded so the number on a customer's own website comes
 * from their configured SLA. A form promising 30 days while the workspace is
 * set to 15 is worse than one that promises nothing.
 */
export function publicRightsTypes(workspace) {
  return call(
    `/public/v1/rights/types?workspace=${encodeURIComponent(workspace)}`,
  );
}

/** Raise a request with no account. Recorded immediately; executes on confirm. */
export function raisePublicRequest({ workspace, type, email, name, details }) {
  return call("/public/v1/rights", {
    method: "POST",
    body: {
      workspace,
      type,
      email,
      name: name || null,
      details: details || null,
    },
  });
}

/**
 * Redeem the emailed token. THIS is what lets the request execute.
 *
 * Both the reference and the token are required: a reference is guessable
 * (DSAR-2026-0001), so the token carries the authority, and asking for the
 * reference too means an intercepted link alone is not enough.
 */
export function confirmPublicRequest({ workspace, reference, token }) {
  return call("/public/v1/rights/confirm", {
    method: "POST",
    body: { workspace, reference, token },
  });
}

/**
 * The snippet a customer pastes into their own website.
 *
 * An iframe rather than injected DOM, and that is the safer choice in both
 * directions: their page cannot read what somebody types into the form, and our
 * markup cannot interfere with theirs. It also means their Content-Security
 * -Policy governs whether we load at all, which is the right party to decide.
 *
 * Generated in the browser rather than served, because it is three lines of
 * HTML with one substitution — an endpoint returning it would be a deployment
 * artefact to keep in step for no benefit.
 */
export function embedSnippet({ workspace, baseUrl }) {
  const origin = baseUrl || window.location.origin;
  const src = `${origin}/rights?workspace=${encodeURIComponent(workspace)}`;
  return `<!-- Data rights request form -->
<iframe
  src="${src}"
  title="Data rights request"
  style="width:100%;min-height:720px;border:0"
  loading="lazy"
></iframe>`;
}

/** The plain link, for a footer or an email signature. */
export function embedLink({ workspace, baseUrl }) {
  const origin = baseUrl || window.location.origin;
  return `${origin}/rights?workspace=${encodeURIComponent(workspace)}`;
}
