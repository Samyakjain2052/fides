// ============================================================================
// Connections to a customer's own systems.
//
// Real, against /v1/connections. Note what this module deliberately does NOT
// have: any way to read a credential back. The server will not return one, so
// there is nothing here to fetch it with — an admin who forgets a key replaces
// it.
//
// The catalogue is fetched rather than duplicated here. The connector list, its
// fields, its help text and its status all live in
// backend/app/connectors/registry.py, so this screen cannot offer a connector
// the backend has never heard of, or show one as usable when its status says
// otherwise. Same reasoning as config/modules.js, applied to integrations.
// ============================================================================
import { apiFetch } from "./auth";

/** Everything the product can connect to, with honest per-connector status. */
export function catalog() {
  return apiFetch("/connections/catalog");
}

/** This workspace's configured connections. Credential hints only. */
export function listConnections() {
  return apiFetch("/connections");
}

export function createConnection({ connectorId, label, values }) {
  return apiFetch("/connections", {
    method: "POST",
    body: { connector_id: connectorId, label, values },
  });
}

/**
 * Edit. A secret left blank means "unchanged".
 *
 * The form cannot display a stored secret, so blank cannot mean "clear it" —
 * clearing a credential is done by deleting the connection.
 */
export function updateConnection(id, { label, values }) {
  return apiFetch(`/connections/${id}`, {
    method: "PATCH",
    body: { label, values },
  });
}

/**
 * Really connect, and record the outcome.
 *
 * The only thing that can move a connection to `connected`. Storing credentials
 * never does.
 */
export function testConnection(id) {
  return apiFetch(`/connections/${id}/test`, { method: "POST" });
}

export function deleteConnection(id) {
  return apiFetch(`/connections/${id}`, { method: "DELETE" });
}

/** Copy for each status. Kept here so the badge and the card agree. */
export const STATUS_COPY = {
  live: {
    label: "Available",
    tone: "success",
    blurb: "Implemented and testable.",
  },
  beta: {
    label: "Beta",
    tone: "warning",
    blurb:
      "Written against the vendor's documentation but never run against a real " +
      "account. Do not rely on it for a statutory deadline yet.",
  },
  planned: {
    label: "Not built yet",
    tone: "neutral",
    blurb:
      "Listed so the shape is agreed. Credentials are refused — there is " +
      "nothing to send them to.",
  },
  needs_oauth: {
    label: "Needs sign-in flow",
    tone: "info",
    blurb:
      "Connects by granting consent in a browser, not by pasting a key. That " +
      "flow is not built yet.",
  },
  needs_agent: {
    label: "Needs an agent",
    tone: "neutral",
    blurb:
      "Cannot be reached from a cloud service. It needs software running " +
      "inside your own network.",
  },
};

export const CONNECTION_STATUS_COPY = {
  connected: { label: "Connected", tone: "success" },
  unverified: { label: "Not tested", tone: "warning" },
  failing: { label: "Failing", tone: "danger" },
  disabled: { label: "Disabled", tone: "neutral" },
};
