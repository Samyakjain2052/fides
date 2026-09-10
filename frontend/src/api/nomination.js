// ============================================================================
// §14 — the right to nominate.
//
// "A Data Principal shall have the right to nominate any other individual, who
// shall, in the event of death or incapacity of the Data Principal, exercise
// the rights of the Data Principal."
//
// There is no GDPR or US-state equivalent, which is why no comparable product
// has these calls.
//
// NOTHING HERE ACTIVATES A NOMINATION. Making one is self-service; invoking one
// is not, and deliberately is not reachable from the data principal's own
// screens — the person who could legitimately invoke it is, by the premise,
// unable to. Activation requires a member of staff to record what evidence they
// saw, and the server refuses the state change without it.
// ============================================================================
import { apiFetch } from "./auth";

/** My own nomination history, and the live one if there is one. */
export function myNomination() {
  return apiFetch("/nominations/mine");
}

/**
 * Nominate somebody.
 *
 * Returns an `acceptance_token` ONCE. It lets the nominee confirm they know
 * about the arrangement, and is never returned again — so if the email does
 * not arrive, this is the only copy.
 */
export function nominate({
  nomineeName,
  nomineeEmail,
  nomineePhone,
  nomineeRelationship,
  scope,
  instructions,
}) {
  return apiFetch("/nominations", {
    method: "POST",
    body: {
      nominee_name: nomineeName,
      nominee_email: nomineeEmail,
      nominee_phone: nomineePhone || null,
      nominee_relationship: nomineeRelationship || null,
      scope: scope || "access_only",
      instructions: instructions || null,
    },
  });
}

/** Withdraw it. No reason required — §14 gives a right to nominate, not a
 *  right for the nominee to remain nominated. */
export function revokeNomination(id, note) {
  return apiFetch(`/nominations/${id}/revoke`, {
    method: "POST",
    body: { note: note || null },
  });
}

/** Staff: every nomination in the workspace. */
export function nominations(status) {
  const q = status ? `?status=${encodeURIComponent(status)}` : "";
  return apiFetch(`/nominations${q}`);
}

/**
 * Staff: activate one, on evidence of death or incapacity.
 *
 * The most consequential call in this client. After it, somebody other than
 * the data principal can obtain or destroy their entire record. The server
 * requires a substantive evidence note — under 20 characters is refused,
 * because "yes" is not a record of what was examined.
 */
export function invokeNomination(id, evidenceNote) {
  return apiFetch(`/nominations/${id}/invoke`, {
    method: "POST",
    body: { evidence_note: evidenceNote },
  });
}

/** Staff: undo an invocation made in error. Recorded as a correction, not as
 *  the principal's decision — they could not have made it. */
export function retractInvocation(id, reason) {
  return apiFetch(`/nominations/${id}/retract-invocation`, {
    method: "POST",
    body: { reason },
  });
}

export const SCOPES = [
  {
    id: "access_only",
    label: "Only obtain a copy of my data",
    hint: "They can ask to see what you held about me. They cannot ask you to delete it. This is the usual choice for somebody settling an estate.",
  },
  {
    id: "access_and_erasure",
    label: "Obtain a copy, and ask for it to be erased",
    hint: "They can see the data and ask you to delete it.",
  },
  {
    id: "all_rights",
    label: "Exercise all of my rights",
    hint: "Everything: see, correct, complete, update and erase.",
  },
];

export const STATUS_COPY = {
  pending: {
    label: "Recorded",
    detail:
      "In force. We have told your nominee it exists; they have not confirmed yet, which does not affect its validity.",
  },
  active: {
    label: "In force",
    detail: "Your nominee knows about it. Nothing happens until it is needed.",
  },
  invoked: {
    label: "In effect",
    detail:
      "Somebody here recorded evidence that it should take effect, and your nominee may now act.",
  },
  revoked: { label: "Withdrawn", detail: "No longer in force." },
  lapsed: { label: "Lapsed", detail: "No longer in force." },
};
