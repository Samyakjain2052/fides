// ============================================================================
// The real consent API. Backed by PostgreSQL, not the mock in ./index.js.
//
// Shapes here match what the existing screens already consume, so switching a
// screen over is an import change rather than a rewrite. Where the server model
// is richer than the old mock (notice versions, legal basis), the extra fields
// are passed through — the screens can start showing them without another round
// of plumbing.
//
// One mapping worth explaining: the signed-in user is an *operator* of the
// console, while consents belong to *Data Principals* — the people whose data a
// customer holds. They are deliberately different tables (see the backend's
// models/consent.py). For "my own preferences" to mean anything, we register
// the signed-in user as a principal of their own organisation, keyed by their
// user id. That keeps the two concepts separate in the data model while giving
// the preference centre a real person to act as.
// ============================================================================
import { apiFetch } from "./auth";

/** The Data Principal representing the signed-in user. Created on first use. */
export async function ensureSelfPrincipal(user) {
  if (!user?.id) throw new Error("Not signed in.");
  // `GET /principals/me`, not `POST /principals`.
  //
  // The POST is a staff route requiring `consent:read`, which a Data Principal
  // does not hold — so this threw 403 for the only role the Preference Centre
  // and Consent History exist to serve. It went unnoticed because those screens
  // were reading mock arrays at the time and rendered invented numbers rather
  // than the error.
  //
  // The server derives the key from the session instead of trusting one sent
  // from here, which also means a caller can no longer name a different
  // `external_id` than their own.
  return apiFetch("/principals/me");
}

export function listPurposes() {
  return apiFetch("/purposes");
}

export function listNotices({ publishedOnly = true } = {}) {
  return apiFetch(`/notices?published_only=${publishedOnly}`);
}

export function listConsents(principalId) {
  return apiFetch(`/consents?principal_id=${encodeURIComponent(principalId)}`);
}

export function grantConsent({ principalId, purposeId, noticeId, method = "checkbox", source }) {
  return apiFetch("/consents", {
    method: "POST",
    body: {
      principal_id: principalId,
      purpose_id: purposeId,
      // Send the version that was actually on screen. If a newer one is
      // published while someone is reading, the record must name the text they
      // saw, not the text that replaced it.
      notice_id: noticeId,
      method,
      source,
    },
  });
}

export function withdrawConsent({ principalId, purposeId, reason }) {
  return apiFetch("/consents/withdraw", {
    method: "POST",
    body: { principal_id: principalId, purpose_id: purposeId, reason },
  });
}

export function checkConsent({ principalId, purposeKey }) {
  return apiFetch(
    `/consents/check?principal_id=${encodeURIComponent(principalId)}` +
      `&purpose=${encodeURIComponent(purposeKey)}`
  );
}

export function consentHistory(principalId) {
  return apiFetch(`/consents/history?principal_id=${encodeURIComponent(principalId)}`);
}

// --------------------------------------------------------------------------- //
// View model
// --------------------------------------------------------------------------- //

/**
 * Everything the preference centre needs, in one call.
 *
 * Returns a row per **purpose**, not per consent — including purposes the person
 * has never answered. A preference centre that only lists existing consents can
 * never be used to give one, which would quietly make "withdraw" the only
 * available action.
 */
export async function preferenceCentre(user) {
  const principal = await ensureSelfPrincipal(user);
  const [purposes, notices, consents] = await Promise.all([
    listPurposes(),
    listNotices({ publishedOnly: true }),
    listConsents(principal.id),
  ]);

  const latestNoticeFor = (purposeId) =>
    notices
      .filter((n) => n.purpose_id === purposeId)
      .sort((a, b) => b.version - a.version)[0] || null;

  const byPurpose = new Map(consents.map((c) => [c.purpose_id, c]));

  const rows = purposes.map((purpose) => {
    const consent = byPurpose.get(purpose.id) || null;
    // The notice the consent was given against — which may be an older version
    // than the current one. Showing the current text next to an old consent
    // would misrepresent what the person agreed to.
    const agreedNotice = consent
      ? {
          id: consent.notice_id,
          version: consent.notice_version,
          content: consent.notice_content,
        }
      : null;
    const currentNotice = latestNoticeFor(purpose.id);

    return {
      purpose,
      consent,
      agreedNotice,
      currentNotice,
      // True when the person agreed to an older version than the one now
      // published. Surfacing it is the honest thing: their agreement is still
      // valid, but it is not agreement to the current wording.
      supersededByNewVersion: Boolean(
        agreedNotice && currentNotice && currentNotice.version > agreedNotice.version
      ),
      daysToExpiry: consent?.expires_at
        ? Math.ceil((new Date(consent.expires_at) - Date.now()) / 864e5)
        : null,
    };
  });

  return { principal, rows };
}

/**
 * Consent history in the shape the history screen already renders.
 *
 * The source is the audit chain, not a history table — so what a person sees
 * here is the same evidence the integrity check verifies, hash and all.
 */
export async function consentHistoryRows(user) {
  const principal = await ensureSelfPrincipal(user);
  const [events, purposes] = await Promise.all([
    consentHistory(principal.id),
    listPurposes(),
  ]);

  const nameFor = new Map(purposes.map((p) => [p.key, p.name]));
  const ACTION = {
    "consent.granted": ["grant", "active"],
    "consent.withdrawn": ["withdraw", "withdrawn"],
    "consent.expired": ["expire", "expired"],
  };

  const rows = events.map((e) => {
    const [action_type, consent_status] = ACTION[e.action] || ["update", "active"];
    const key = e.payload.purpose_key || "";
    return {
      log_id: `SEQ-${String(e.seq).padStart(4, "0")}`,
      purpose_id: key,
      purpose: nameFor.get(key) || key,
      action_type,
      consent_status,
      timestamp: e.occurred_at,
      method: e.payload.method || "—",
      version: e.payload.notice_version ?? "—",
      language: e.payload.language || "English",
      initiator: e.actor_type,
      audit_hash: e.hash,
    };
  });

  return { rows, purposes };
}

/**
 * Consent totals for the dashboard.
 *
 * `overview.active` counts consents the product will actually honour — status
 * active AND expiry not passed, the same judgement `/consents/check` makes.
 * `lapsed_not_yet_marked` is the gap: rows still reading active that validation
 * would already refuse.
 *
 * Do not derive these in the browser from a list. The list is paginated, expiry
 * is evaluated server-side against the clock, and a figure computed here would
 * drift from the one the validation endpoint enforces.
 */
export function consentOverview() {
  return apiFetch("/consents/overview");
}
