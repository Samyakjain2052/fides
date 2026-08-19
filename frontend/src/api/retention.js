// ============================================================================
// Retention policies and the purge executor.
//
// The only part of this product that destroys data, so the client mirrors the
// server's caution rather than smoothing it over:
//
//   * `preview` is the primary call. It is what the screen leads with.
//   * `runPurge` demands the policy name back verbatim, because an irreversible
//     action needs a step that cannot be taken by a mis-click.
//   * A receipt is fetched after either, and skips are surfaced with their
//     reasons — "not purged because they have an open rights request" is the
//     answer to a question somebody will eventually ask.
// ============================================================================
import { apiFetch } from "./auth";

export function listPolicies() {
  return apiFetch("/retention/policies");
}

export function createPolicy(body) {
  return apiFetch("/retention/policies", {
    method: "POST",
    body: {
      name: body.name,
      data_category: body.dataCategory,
      retention_days: Number(body.retentionDays),
      action: body.action || "mask",
      auto_delete: Boolean(body.autoDelete),
      notify_days: Number(body.notifyDays ?? 14),
      exemption_code: body.exemptionCode || "none",
      exemption_reference: body.exemptionReference || null,
    },
  });
}

/** Dry run. Reports exactly what WOULD happen and touches nothing. */
export function preview(policyId) {
  return apiFetch(`/retention/policies/${policyId}/preview`, { method: "POST" });
}

/**
 * LIVE run. Irreversible.
 *
 * `confirm` must be the policy's name exactly; the server refuses otherwise.
 * That is deliberate friction, and the UI should not paper over it by filling
 * the field in automatically.
 */
export function runPurge(policyId, confirm) {
  return apiFetch(`/retention/policies/${policyId}/run`, {
    method: "POST",
    body: { confirm },
  });
}

export function listRuns(policyId) {
  const q = policyId ? `?policy_id=${policyId}` : "";
  return apiFetch(`/retention/runs${q}`);
}

export function runItems(runId) {
  return apiFetch(`/retention/runs/${runId}/items`);
}

/**
 * Edit a policy.
 *
 * A PATCH: omit a field to leave it alone. `dataCategory` is deliberately not
 * accepted — the server refuses it, because repointing a policy at another
 * category would carry its history and its purge receipts to a different set of
 * people. Create a new policy for that.
 *
 * `confirmShortening` is required when shortening the window on an auto-delete
 * policy. That edit enlarges an unattended destruction set without anybody
 * pressing anything, which is the one consequence here that should not follow from
 * an ordinary form save. Preview first, then confirm.
 */
export function updatePolicy(policyId, patch) {
  const body = {};
  const map = {
    name: "name",
    retentionDays: "retention_days",
    action: "action",
    autoDelete: "auto_delete",
    notifyDays: "notify_days",
    exemptionCode: "exemption_code",
    exemptionReference: "exemption_reference",
    isActive: "is_active",
  };
  for (const [from, to] of Object.entries(map)) {
    if (patch[from] !== undefined && patch[from] !== "") body[to] = patch[from];
  }
  if (body.retention_days !== undefined) body.retention_days = Number(body.retention_days);
  if (body.notify_days !== undefined) body.notify_days = Number(body.notify_days);
  body.confirm_shortening = Boolean(patch.confirmShortening);
  return apiFetch(`/retention/policies/${policyId}`, { method: "PATCH", body });
}
