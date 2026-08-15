// ============================================================================
// The public consent banner API.
//
// Deliberately NOT using `apiFetch` from ./auth.js. That sends a Bearer token
// and `credentials: "include"`, and these pages have neither — a first-time
// visitor meets the banner before they have an account. This is a plain fetch
// with a publishable key, which is a different kind of caller entirely.
//
// The key is PUBLIC by design. It ships in this bundle, anyone can read it, and
// that is fine: it can only collect consent (never withdraw, never read), it is
// pinned to an origin allowlist, and every record it creates is stamped with
// server-observed provenance. See docs/PUBLISHABLE_KEY_SECURITY.md.
// ============================================================================

const BASE = "/public/v1/banner";

/**
 * Where the publishable key comes from, in precedence order:
 *
 *   1. `window.DataShieldConfig.publishableKey` — what a real customer embed
 *      would set from their own page.
 *   2. `?pk=` — lets one deployment demo several workspaces without a rebuild.
 *   3. `VITE_PUBLISHABLE_KEY` — baked in at build time, the default.
 *
 * Returns null rather than throwing so the caller can render an honest
 * "not configured" state instead of a blank screen.
 */
export function publishableKey() {
  const injected = globalThis.window?.DataShieldConfig?.publishableKey;
  if (injected) return injected;

  const fromQuery = new URLSearchParams(globalThis.location?.search || "").get("pk");
  if (fromQuery) return fromQuery;

  return import.meta.env.VITE_PUBLISHABLE_KEY || null;
}

const VISITOR_KEY = "datashield.visitor_id";

/**
 * Who this consent belongs to.
 *
 * A signed-in person is themselves. An anonymous visitor gets a random id kept
 * in localStorage — the honest "this browser" identifier, and the only thing
 * that lets them see their own choices again on a later visit.
 *
 * Without a signed token this reference is ASSERTED, not verified: the server
 * records it that way (`strongly_bound: false`) rather than pretending
 * otherwise. For sensitive purposes an integrator uses the signed-token
 * step-up, which is why `consentToken` is a parameter below.
 */
export function principalRef(user) {
  if (user?.id) return `user:${user.id}`;

  let id = null;
  try {
    id = localStorage.getItem(VISITOR_KEY);
    if (!id) {
      id = `visitor:${crypto.randomUUID()}`;
      localStorage.setItem(VISITOR_KEY, id);
    }
  } catch {
    // Private browsing, or storage disabled. A per-session id is worse than a
    // persistent one — they will not see these choices again — but it is much
    // better than refusing to record consent at all.
    id = `visitor:${crypto.randomUUID()}`;
  }
  return id;
}

/** One key per banner mount, so a double-click cannot record two consents. */
export function newIdempotencyKey() {
  return `banner-${crypto.randomUUID()}`;
}

class BannerError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function call(path, { method = "GET", body, idempotencyKey } = {}) {
  const key = publishableKey();
  if (!key) {
    throw new BannerError(
      "This banner has no publishable key configured, so consent cannot be " +
        "recorded. Set VITE_PUBLISHABLE_KEY at build time.",
      0,
      null
    );
  }

  const headers = { "X-Publishable-Key": key };
  if (body) headers["Content-Type"] = "application/json";
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;

  const resp = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  const text = await resp.text();
  let parsed = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    // A non-JSON body from an API means something is routing wrongly — most
    // likely the SPA fallback answering a machine path. Say that, rather than
    // letting a JSON parse error surface as a mystery.
    throw new BannerError(
      `The consent API returned a non-JSON response (HTTP ${resp.status}). ` +
        "Check that /public/ is proxied to the backend.",
      resp.status,
      text.slice(0, 200)
    );
  }

  if (!resp.ok) {
    // The server gives real reasons — 403 for a disallowed origin, 409 for a
    // mandatory purpose, 429 for a rate limit. Passing them through turns a
    // fixable integration mistake into a fixable integration mistake, instead
    // of "the banner doesn't work".
    throw new BannerError(
      parsed?.detail || parsed?.title || `Request failed (${resp.status})`,
      resp.status,
      parsed
    );
  }
  return parsed;
}

/**
 * The purposes a banner may offer, with the exact notice wording to show.
 *
 * Mandatory purposes are excluded by the server — they do not rest on consent,
 * and offering a toggle for one is the dark pattern the DPDP Act is written
 * against. Purposes with no published notice are excluded too, because consent
 * cannot lawfully be collected against text that was never published.
 */
export function bannerPurposes() {
  return call("/purposes");
}

/**
 * Record one purpose's consent.
 *
 * There is no `granted: false`. Declining means *not collecting* consent, not
 * withdrawing it — a publishable key cannot withdraw, and the banner must not
 * appear to offer something it cannot do. Withdrawal lives in the Preference
 * Centre, behind a real session.
 */
export function collectConsent({
  principalRef: ref,
  purpose,
  language = "English",
  source,
  consentToken,
  idempotencyKey,
}) {
  return call("/consent", {
    method: "POST",
    idempotencyKey,
    body: {
      principal_ref: ref,
      purpose,
      language,
      source,
      ...(consentToken ? { consent_token: consentToken } : {}),
    },
  });
}

export { BannerError };
