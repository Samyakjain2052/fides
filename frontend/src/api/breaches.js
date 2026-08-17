// ============================================================================
// The breach register — DPDP §8(6).
//
// Two things this client cannot do, because the server cannot:
//
//   * **Submit to the Data Protection Board.** `boardNotice()` returns text for a
//     human to paste into the Board's portal; `notifyBoard()` records that a named
//     person did so, and the reference they got back. There is no automated
//     submission — unattended software contacting a regulator is not something
//     this product does, and the endpoint names say the closest thing to a lie in
//     the module, so the UI has to be explicit.
//   * **Delete anything.** A mistaken entry is voided with a reason and kept. A
//     register whose entries can vanish is not a register.
//
// `notifyPrincipals` is resumable and must be called in a loop. Ten thousand
// people and a provider rate limit means one call will not finish, and every
// attempt is recorded per person, so calling again never double-notifies.
// ============================================================================
import { apiFetch } from "./auth";

export const SEVERITIES = [
  { id: "low", label: "Low" },
  { id: "medium", label: "Medium" },
  { id: "high", label: "High" },
  { id: "critical", label: "Critical" },
];

export const STATUS_LABEL = {
  draft: "Draft",
  investigating: "Investigating",
  contained: "Contained",
  notified: "Notified",
  closed: "Closed",
  void: "Voided",
};

export function listBreaches({ status, severity, openOnly, limit = 100 } = {}) {
  const q = new URLSearchParams();
  if (status) q.set("status", status);
  if (severity) q.set("severity", severity);
  if (openOnly) q.set("open_only", "true");
  q.set("limit", String(limit));
  return apiFetch(`/breaches?${q}`);
}

export function getBreach(id) {
  return apiFetch(`/breaches/${id}`);
}

export function recordBreach(body) {
  return apiFetch("/breaches", {
    method: "POST",
    body: {
      title: body.title,
      description: body.description,
      severity: body.severity || "medium",
      // The statutory clock runs from this, not from when the breach happened.
      discovered_at: body.discoveredAt || null,
      occurred_at: body.occurredAt || null,
      categories_affected: body.categories || [],
      estimated_affected_count:
        body.estimatedCount === "" || body.estimatedCount == null
          ? null
          : Number(body.estimatedCount),
    },
  });
}

/**
 * Update narrative fields, and `discovered_at` only with a reason.
 *
 * The server refuses a change to an already-recorded awareness date without one.
 * Do not paper over that: it moves the deadline the fiduciary is judged against,
 * and both values go into the audit chain.
 */
export function updateBreach(id, patch) {
  const body = {};
  const map = {
    title: "title",
    description: "description",
    severity: "severity",
    discoveredAt: "discovered_at",
    discoveredAtReason: "discovered_at_reason",
    occurredAt: "occurred_at",
    containedAt: "contained_at",
    categories: "categories_affected",
    rootCause: "root_cause",
    remediation: "remediation",
  };
  for (const [from, to] of Object.entries(map)) {
    if (patch[from] !== undefined && patch[from] !== "") body[to] = patch[from];
  }
  if (patch.estimatedCount !== undefined && patch.estimatedCount !== "") {
    body.estimated_affected_count = Number(patch.estimatedCount);
  }
  return apiFetch(`/breaches/${id}`, { method: "PATCH", body });
}

/** Only `investigating` and `contained`. Everything else has its own action. */
export function changeStatus(id, toStatus, note) {
  return apiFetch(`/breaches/${id}/status`, {
    method: "POST",
    body: { to_status: toStatus, note: note || null },
  });
}

/**
 * Who a category query would attach. Sends nothing, attaches nothing.
 *
 * Always run this before attaching. Notifying the wrong people about a breach is
 * itself an incident, and it is not one you can undo.
 */
export function previewAffected(id, categories) {
  return apiFetch(`/breaches/${id}/affected/preview`, {
    method: "POST",
    body: { categories },
  });
}

export function attachAffected(id, { categories, principalIds } = {}) {
  return apiFetch(`/breaches/${id}/affected`, {
    method: "POST",
    body: {
      categories: categories || null,
      principal_ids: principalIds || null,
    },
  });
}

export function listAffected(id, { offset = 0, limit = 200 } = {}) {
  return apiFetch(`/breaches/${id}/affected?offset=${offset}&limit=${limit}`);
}

/** The text to submit by hand. Generated, never transmitted. */
export function boardNotice(id) {
  return apiFetch(`/breaches/${id}/board-notice`);
}

export function notifyBoard(id, { submittedBy, boardReference, submittedAt } = {}) {
  return apiFetch(`/breaches/${id}/notify-board`, {
    method: "POST",
    body: {
      submitted_by: submittedBy,
      board_reference: boardReference || null,
      submitted_at: submittedAt || null,
    },
  });
}

/** One batch. Call repeatedly until `progress.complete`. */
export function notifyPrincipals(id, batch = 100) {
  return apiFetch(`/breaches/${id}/notify-principals?batch=${batch}`, {
    method: "POST",
  });
}

export function closeBreach(id, { rootCause, remediation, exemption } = {}) {
  return apiFetch(`/breaches/${id}/close`, {
    method: "POST",
    body: {
      root_cause: rootCause,
      remediation,
      notification_exemption: exemption || null,
    },
  });
}

export function voidBreach(id, reason) {
  return apiFetch(`/breaches/${id}/void`, { method: "POST", body: { reason } });
}
