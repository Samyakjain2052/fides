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
