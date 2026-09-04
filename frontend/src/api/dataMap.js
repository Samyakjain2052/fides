// ============================================================================
// One person's data, across the connected systems.
//
// Reached from a rights request and scoped to it. There is deliberately no
// "look up any person" call here: the only way in is a request that names them,
// which is what stops this becoming a customer-data browser.
//
// The map is METADATA — systems, tables, row counts, categories, and which
// column matched. No values. A rights request authorises acting on somebody's
// data, not reading it, and an admin browsing a full record because a request
// arrived is processing it for a new purpose.
// ============================================================================
import { apiFetch } from "./auth";

/** Where this person's data is. Read-only; queries the customer's systems. */
export function dataMap(requestId) {
  return apiFetch(`/dsar/${requestId}/data-map`);
}

/**
 * Mask this person out of the connected systems. Irreversible.
 *
 * `confirmReference` must be the request's own reference, typed back — the same
 * guard the retention live run uses, because an action with no undo should not
 * follow from a single click.
 *
 * `only` optionally names "<connectionId>:<table>" entries, so a table that
 * must be retained for a statutory reason can be left out.
 */
export function eraseAcrossSystems(requestId, { confirmReference, only }) {
  return apiFetch(`/dsar/${requestId}/erase`, {
    method: "POST",
    body: { confirm_reference: confirmReference, only },
  });
}

/** Categories where erasure is often unlawful rather than merely awkward. */
export const STATUTORY_CATEGORIES = ["Financial", "Government ID", "Health"];
