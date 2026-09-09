// ============================================================================
// Per-system work on a rights request.
//
// The data map answers "where is this person's data" in one sweep. These are
// the pieces of work that come out of it, each with an owner and each closed by
// somebody saying what they concluded.
//
// The important call here is `close`, and the important argument is
// `attestation`. It is required — including for a nil result — because "we
// searched payroll and it held nothing about this person" is a finding, and
// recording it as one is the only thing that distinguishes it from "nobody
// looked at payroll". A queue that simply shows no rows expresses both.
// ============================================================================
import { apiFetch } from "./auth";

/** Every item on this request, plus the counts a screen needs. */
export function actionItems(requestId) {
  return apiFetch(`/dsar/${requestId}/action-items`);
}

/**
 * Create one item per connected system. Safe to press twice.
 *
 * Idempotent server-side: a re-run after a connection is added creates only the
 * missing items and never disturbs work somebody has claimed. Connections get
 * added mid-request, so re-running is normal rather than exceptional.
 */
export function fanOut(requestId) {
  return apiFetch(`/dsar/${requestId}/action-items/fan-out`, { method: "POST" });
}

/** An item for a system this product cannot reach — a bureau, an archive. */
export function addManualItem(requestId, { systemLabel, assigneeUserId }) {
  return apiFetch(`/dsar/${requestId}/action-items`, {
    method: "POST",
    body: { system_label: systemLabel, assignee_user_id: assigneeUserId || null },
  });
}

/** Assign, reassign, or (with null) hand back to unassigned. */
export function assignItem(requestId, itemId, assigneeUserId) {
  return apiFetch(`/dsar/${requestId}/action-items/${itemId}/assign`, {
    method: "PATCH",
    body: { assignee_user_id: assigneeUserId || null },
  });
}

export function claimItem(requestId, itemId) {
  return apiFetch(`/dsar/${requestId}/action-items/${itemId}/claim`, {
    method: "POST",
  });
}

/**
 * Close with a stated conclusion.
 *
 * `basis` is required when `outcome` is "retained": keeping somebody's data
 * against their erasure request is a legal claim and needs a stated ground.
 */
export function closeItem(
  requestId,
  itemId,
  { outcome, attestation, recordsFound = 0, basis, internalNotes },
) {
  return apiFetch(`/dsar/${requestId}/action-items/${itemId}/close`, {
    method: "POST",
    body: {
      outcome,
      attestation,
      records_found: recordsFound,
      basis: basis || null,
      internal_notes: internalNotes || null,
    },
  });
}

export function skipItem(requestId, itemId, reason) {
  return apiFetch(`/dsar/${requestId}/action-items/${itemId}/skip`, {
    method: "POST",
    body: { reason },
  });
}

/** Undo a close. The superseded conclusion stays in the audit chain. */
export function reopenItem(requestId, itemId, reason) {
  return apiFetch(`/dsar/${requestId}/action-items/${itemId}/reopen`, {
    method: "POST",
    body: { reason },
  });
}

/**
 * Record that somebody outside was told to act. Sends nothing.
 *
 * §8(2) keeps the fiduciary responsible for its processors, so "we asked our
 * vendor to delete it" is a fact worth producing with a date on it. What
 * actually reaches a processor is a contractual matter and frequently not
 * email, so this records rather than delivers.
 */
export function recordThirdParty(requestId, itemId, { address, note }) {
  return apiFetch(`/dsar/${requestId}/action-items/${itemId}/third-party`, {
    method: "POST",
    body: { address, note: note || null },
  });
}

/** The outcomes, with the wording a person picking one should read. */
export const OUTCOMES = [
  {
    id: "no_records_matched",
    label: "Searched — nothing matched",
    hint: "This system holds no data about this person. Recorded as a finding, not as silence.",
  },
  {
    id: "data_found",
    label: "Found and disclosed",
    hint: "Records matched and were included in the response.",
  },
  {
    id: "erased",
    label: "Found and erased",
    hint: "Records matched and were masked or deleted.",
  },
  {
    id: "retained",
    label: "Found, but being kept",
    hint: "Needs the obligation that requires you to keep it.",
  },
  {
    id: "third_party_asked",
    label: "Asked somebody else to act",
    hint: "We cannot reach this system; a processor or team was told.",
  },
];
