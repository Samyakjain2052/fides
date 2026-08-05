// ============================================================================
// The real audit trail. HMAC-SHA256 hash-chained, append-only, in PostgreSQL.
//
// This is the screen a customer's auditor and a regulator actually look at, so
// two things matter more here than anywhere else in the app:
//
//   1. Nothing on it may be invented. The previous version generated an export
//      "signature" from `rowCount * 7919` — a number that looks like a hash and
//      proves nothing. The chain's real head hash goes in the export instead.
//   2. A failed integrity check must look like a failure. The previous version
//      rendered "Integrity check passed" unconditionally; a tamper-evident trail
//      whose UI cannot show tampering is not tamper-evident to anyone reading it.
// ============================================================================
import { apiFetch } from "./auth";

/** One page of the trail, newest first. `beforeSeq` is the cursor. */
export function listAuditEvents({ action, entityType, actorId, beforeSeq, limit = 100 } = {}) {
  const q = new URLSearchParams();
  if (action) q.set("action", action);
  if (entityType) q.set("entity_type", entityType);
  if (actorId) q.set("actor_id", actorId);
  if (beforeSeq) q.set("before_seq", String(beforeSeq));
  q.set("limit", String(limit));
  return apiFetch(`/audit?${q}`);
}

/**
 * Walk the chain and report the first break.
 *
 * What this catches: an edited entry (its hash no longer matches its contents),
 * a deleted one (the next entry's prev_hash no longer matches), a reordering.
 * What it cannot catch is truncation of the newest entries — that needs external
 * anchoring, which is Phase 10 and is stated as an open gap rather than glossed.
 */
export function verifyChain() {
  return apiFetch("/audit/verify", { method: "POST" });
}

// Server action -> the vocabulary the screen filters on. Unmapped actions fall
// through with their raw name rather than being dropped: a trail that hides
// entries it does not recognise is worse than one showing an ugly label.
const ACTION_LABEL = {
  "auth.login_succeeded": "login",
  "auth.login_failed": "login_failed",
  "auth.logout": "logout",
  "auth.token_refreshed": "token_refreshed",
  "auth.token_reuse_detected": "token_reuse_detected",
  "auth.account_locked": "account_locked",
  "tenant.created": "tenant_created",
  "tenant.updated": "config_changed",
  "user.created": "user_created",
  "user.role_changed": "role_changed",
  "apikey.created": "apikey_created",
  "apikey.revoked": "apikey_revoked",
  "consent.granted": "grant",
  "consent.withdrawn": "withdraw",
  "consent.expired": "expire",
  "consent.validated": "validate",
  "notice.published": "notice_published",
  "dsar.submitted": "dsar_submitted",
  "dsar.status_changed": "dsar_status_changed",
  "dsar.completed": "dsar_completed",
  "audit.verified": "integrity_verified",
  "audit.integrity_failed": "integrity_failed",
};

/**
 * Events in the shape the screen already renders.
 *
 * `log_id` is the chain sequence, not a random id: seq is what makes a gap
 * visible to someone reading the table, which is the whole point of a chain.
 */
export function toRow(e) {
  return {
    id: e.id,
    seq: e.seq,
    log_id: `SEQ-${String(e.seq).padStart(5, "0")}`,
    timestamp: e.created_at,
    action_type: ACTION_LABEL[e.action] || e.action,
    raw_action: e.action,
    // The person or machine responsible. `actor_label` is the email or key name;
    // falling back to the id keeps the row attributable either way.
    user_id: e.actor_label || e.actor_id || "—",
    initiator: e.actor_type,
    purpose_id: e.payload?.purpose_key || "",
    consent_status: e.payload?.status || statusFor(e.action),
    entity: e.entity_type ? `${e.entity_type}:${String(e.entity_id || "").slice(0, 8)}` : "—",
    source_ip: e.ip_address || "—",
    audit_hash: e.hash,
    prev_hash: e.prev_hash,
    payload: e.payload || {},
  };
}

function statusFor(action) {
  if (action === "consent.granted") return "active";
  if (action === "consent.withdrawn") return "withdrawn";
  if (action === "consent.expired") return "expired";
  return "";
}

export async function auditTrail(opts = {}) {
  const page = await listAuditEvents(opts);
  return {
    rows: page.items.map(toRow),
    total: page.total,
    nextCursor: page.next_cursor,
    // Distinct actions actually present, so the filter offers what exists
    // rather than a hardcoded list that drifts from the server's vocabulary.
    actions: [...new Set(page.items.map((e) => ACTION_LABEL[e.action] || e.action))].sort(),
    initiators: [...new Set(page.items.map((e) => e.actor_type))].sort(),
  };
}
