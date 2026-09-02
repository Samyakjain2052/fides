// ============================================================================
// Connections (/admin/connections)
//
// Where an admin points this product at their own systems.
//
// THE FORM IS GENERATED, NOT WRITTEN. Every field, label, placeholder, help
// string and status badge comes from GET /v1/connections/catalog, which serves
// backend/app/connectors/registry.py verbatim. Nothing about a connector is
// described twice, so this screen cannot invent a connector, cannot offer a
// field the backend will reject, and cannot show something as available when the
// registry says it is not.
//
// TWO HONESTY RULES, both enforced by the server and mirrored here so the UI
// does not have to be trusted:
//
//   1. Storing a credential is not connecting. A new connection reads
//      "Not tested" until a real probe succeeds. The Test button is the only
//      thing that can turn it green.
//   2. A connector that cannot use a credential does not get a form. The server
//      refuses those anyway — holding somebody's live Razorpay key for an
//      integration that does not exist is a risk with no feature attached — but
//      showing an inviting form and then refusing the submit would be a worse
//      experience than not showing one.
//
// Credentials are never displayed. The server will not return one; the most an
// admin sees back is a last-4 hint, enough to recognise which key they pasted.
//
// NO PROBE ON MOUNT, DELIBERATELY. The obvious design is to test every
// connection when this page opens. A probe is a real connection to the
// customer's production system using their credentials, so on mount that means a
// refresh authenticates against their database again, ten connections is ten
// simultaneous production connections or eighty seconds of waiting at the 8s
// timeout, and once the API connectors exist every page view spends their rate
// limit. The deciding objection is different: a page-load check is only correct
// while somebody is looking at the page, and the point of monitoring is knowing
// a connection broke at 3am — before a rights request arrives and its statutory
// clock starts. So `connections.healthcheck` probes every 15 minutes in the
// background, this page reads the recorded result instantly, and the age of that
// result is shown rather than hidden.
// ============================================================================
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CONNECTION_STATUS_COPY,
  STATUS_COPY,
  catalog as fetchCatalog,
  createConnection,
  deleteConnection,
  listConnections,
  testConnection,
} from "../../api/connections";
import { useApp } from "../../context/AppContext";

/**
 * How old a health result is, in words.
 *
 * Shown because a green badge from four hours ago and one from four minutes ago
 * are different claims, and a page that renders both identically is quietly
 * lying about the second.
 */
function freshness(iso) {
  if (!iso) return { text: "never checked", stale: true };
  const minutes = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return { text: "checked just now", stale: false };
  if (minutes < 60) return { text: `checked ${minutes}m ago`, stale: minutes > 45 };
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return { text: `checked ${hours}h ago`, stale: true };
  return { text: `checked ${Math.floor(hours / 24)}d ago`, stale: true };
}

const TONE_DOT = {
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger",
  info: "bg-info",
  neutral: "bg-muted",
};

function Badge({ tone, children }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-line bg-surface px-2 py-0.5 text-xs text-ink">
      {/* A dot AND a label, never colour alone. */}
      <span
        className={`h-2 w-2 shrink-0 rounded-full ${TONE_DOT[tone] || "bg-muted"}`}
        aria-hidden="true"
      />
      {children}
    </span>
  );
}

export default function Connections() {
  const { notify } = useApp();

  const [cat, setCat] = useState(null);
  const [rows, setRows] = useState([]);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState(null); // the connector being configured
  const [form, setForm] = useState({});
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(null);
  const [showUnavailable, setShowUnavailable] = useState(false);

  const load = useCallback(async () => {
    try {
      const [c, r] = await Promise.all([fetchCatalog(), listConnections()]);
      setCat(c);
      setRows(r);
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const grouped = useMemo(() => {
    if (!cat) return [];
    const items = showUnavailable
      ? cat.items
      : cat.items.filter((i) => i.storable);
    const byCategory = new Map();
    for (const item of items) {
      if (!byCategory.has(item.category)) byCategory.set(item.category, []);
      byCategory.get(item.category).push(item);
    }
    return [...byCategory.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [cat, showUnavailable]);

  const openForm = (connector) => {
    setAdding(connector);
    setLabel("");
    // Seed defaults from the registry so the admin is not retyping port 5432.
    setForm(
      Object.fromEntries(
        connector.fields.map((f) => [f.key, f.default ?? ""]),
      ),
    );
    setError("");
  };

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await createConnection({
        connectorId: adding.id,
        label: label.trim() || adding.label,
        values: form,
      });
      notify(
        `${adding.label} saved. It is not verified yet — use Test to check it ` +
          `actually connects.`,
      );
      setAdding(null);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const runTest = async (row) => {
    setTesting(row.id);
    try {
      const result = await testConnection(row.id);
      notify(
        result.last_test_ok
          ? `${row.label}: connected.`
          : `${row.label}: ${result.last_test_message}`,
        result.last_test_ok ? "success" : "error",
      );
      await load();
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setTesting(null);
    }
  };

  const remove = async (row) => {
    setBusy(true);
    try {
      await deleteConnection(row.id);
      notify(`${row.label} removed, along with its credential.`);
      await load();
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setBusy(false);
    }
  };

  if (!cat) {
    return <p className="text-sm text-muted">Loading connectors…</p>;
  }

  const { counts } = cat;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-ink">Connections</h1>
        <p className="text-sm text-muted">
          Point DataShield at the systems that hold your customers&rsquo; data,
          so a rights request can reach them.
        </p>
      </div>

      {/* The honest headline. Forty logos and three working connectors is the
          truth, and burying it would make every card look equally ready. */}
      <div className="card border-info/40 bg-info/5 p-4 text-sm">
        <p className="text-ink">
          <strong>{counts.storable} of {counts.total}</strong> connectors accept
          credentials today. The rest are listed so you can see what is coming
          and how each one will need to connect.
        </p>
        <p className="mt-1 text-xs text-muted">
          {counts.by_status.needs_oauth} connect by signing in rather than by
          pasting a key ({" "}
          <span className="text-ink">that flow is not built yet</span>);{" "}
          {counts.by_status.planned} are not implemented;{" "}
          {counts.by_status.needs_agent} cannot be reached from a cloud service
          at all without software inside your network.
        </p>
      </div>

      {error && (
        <div className="rounded-lg border border-danger/50 bg-danger/10 p-3 text-sm text-ink">
          {error}
        </div>
      )}

      {/* ------------------------------------------------ configured ------ */}
      <section className="card overflow-hidden">
        <div className="border-b border-line px-5 py-4">
          <h2 className="font-semibold text-ink">Your connections</h2>
          <p className="text-xs text-muted">
            A connection is only <strong>Connected</strong> once a test has
            actually reached the system. Storing a password proves nothing.
          </p>
          <p className="mt-1 text-xs text-muted">
            Checked automatically every 15 minutes in the background, so this
            page loads instantly and a connection that breaks overnight is
            noticed before a rights request arrives. Each row shows how old its
            result is. <strong>Test</strong> checks one immediately.
          </p>
        </div>

        {rows.length === 0 ? (
          <p className="px-5 py-6 text-sm text-muted">
            Nothing connected yet. Add one from the catalogue below.
          </p>
        ) : (
          <ul className="divide-y divide-line">
            {rows.map((row) => {
              const copy =
                CONNECTION_STATUS_COPY[row.status] || {
                  label: row.status,
                  tone: "neutral",
                };
              return (
                <li key={row.id} className="px-5 py-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="font-medium text-ink">
                        {row.label}{" "}
                        <span className="text-xs text-muted">
                          · {row.connector_label}
                        </span>
                      </p>
                      <div className="mt-1 flex flex-wrap items-center gap-2">
                        <Badge tone={copy.tone}>{copy.label}</Badge>
                        {(() => {
                          const f = freshness(row.last_tested_at);
                          return (
                            <span
                              className={`text-xs ${f.stale ? "text-warning" : "text-muted"}`}
                              title={
                                row.last_tested_at
                                  ? new Date(row.last_tested_at).toLocaleString()
                                  : "no check has run yet"
                              }
                            >
                              {f.text}
                            </span>
                          );
                        })()}
                        {row.consecutive_failures > 1 && (
                          <span className="text-xs text-danger">
                            {row.consecutive_failures} checks in a row
                          </span>
                        )}
                        {!row.monitor && (
                          <span className="text-xs text-muted">
                            monitoring off
                          </span>
                        )}
                      </div>
                    </div>
                    <div className="flex shrink-0 gap-2">
                      <button
                        type="button"
                        className="btn-secondary text-sm"
                        onClick={() => runTest(row)}
                        disabled={testing === row.id}
                      >
                        {testing === row.id ? "Testing…" : "Test"}
                      </button>
                      <button
                        type="button"
                        className="btn-ghost text-sm text-danger"
                        onClick={() => remove(row)}
                        disabled={busy}
                      >
                        Remove
                      </button>
                    </div>
                  </div>

                  {/* Non-secret config in clear, secrets as a last-4 hint. */}
                  <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-xs">
                    {Object.entries(row.config || {}).map(([k, v]) => (
                      <div key={k} className="flex gap-1">
                        <dt className="text-muted">{k}</dt>
                        <dd className="font-mono text-ink">{String(v)}</dd>
                      </div>
                    ))}
                    {Object.entries(row.hints || {}).map(([k, v]) => (
                      <div key={k} className="flex gap-1">
                        <dt className="text-muted">{k}</dt>
                        <dd className="font-mono text-ink">{v}</dd>
                      </div>
                    ))}
                  </dl>

                  {row.last_test_ok === false && row.last_test_message && (
                    <p className="mt-2 rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-xs text-ink">
                      {/* The vendor's own words, not a paraphrase. */}
                      {row.last_test_message}
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      {/* -------------------------------------------------- catalogue ------ */}
      <section className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="font-semibold text-ink">Available connectors</h2>
          <label className="flex cursor-pointer items-center gap-2 text-sm text-muted">
            <input
              type="checkbox"
              checked={showUnavailable}
              onChange={(e) => setShowUnavailable(e.target.checked)}
            />
            Show the {counts.total - counts.storable} that are not ready
          </label>
        </div>

        {grouped.map(([category, items]) => (
          <div key={category}>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
              {category}
            </h3>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {items.map((item) => {
                const copy = STATUS_COPY[item.status] || {
                  label: item.status,
                  tone: "neutral",
                  blurb: "",
                };
                return (
                  <div key={item.id} className="card flex flex-col p-4">
                    <div className="flex items-start justify-between gap-2">
                      <p className="font-medium text-ink">{item.label}</p>
                      <Badge tone={copy.tone}>{copy.label}</Badge>
                    </div>

                    <p className="mt-2 flex-1 text-xs text-muted">
                      {item.note || copy.blurb}
                    </p>

                    {item.capabilities.length > 0 && (
                      <p className="mt-2 text-xs text-muted">
                        {item.capabilities.join(" · ")}
                      </p>
                    )}

                    {/* No button at all when it cannot accept credentials. An
                        inviting form that the server then refuses is worse than
                        no form. */}
                    {item.storable ? (
                      <button
                        type="button"
                        className="btn-secondary mt-3 text-sm"
                        onClick={() => openForm(item)}
                      >
                        Add connection
                      </button>
                    ) : (
                      <p className="mt-3 text-xs italic text-muted">
                        Not configurable yet.
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </section>

      {/* ------------------------------------------------------- form ------ */}
      {adding && (
        <div
          className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-ink/40 p-4"
          role="dialog"
          aria-modal="true"
          aria-label={`Add a ${adding.label} connection`}
        >
          <form
            onSubmit={submit}
            className="card my-8 w-full max-w-lg space-y-4 p-5"
          >
            <div>
              <h2 className="font-semibold text-ink">
                Connect {adding.label}
              </h2>
              <p className="text-xs text-muted">
                Credentials are encrypted before they are stored and are never
                shown again — you will see only the last four characters.
              </p>
            </div>

            <div>
              <label className="label" htmlFor="conn-label">
                Name this connection
              </label>
              <input
                id="conn-label"
                className="input"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                placeholder={adding.label}
              />
              <p className="mt-1 text-xs text-muted">
                So you can tell two {adding.label} connections apart later.
              </p>
            </div>

            {/* Generated from the registry. */}
            {adding.fields.map((f) => (
              <div key={f.key}>
                <label className="label" htmlFor={`f-${f.key}`}>
                  {f.label}
                  {!f.required && (
                    <span className="ml-1 text-xs font-normal text-muted">
                      (optional)
                    </span>
                  )}
                </label>

                {f.kind === "select" ? (
                  <select
                    id={`f-${f.key}`}
                    className="input"
                    value={form[f.key] ?? ""}
                    onChange={(e) =>
                      setForm({ ...form, [f.key]: e.target.value })
                    }
                  >
                    {f.options.map((o) => (
                      <option key={o} value={o}>
                        {o}
                      </option>
                    ))}
                  </select>
                ) : f.kind === "textarea" ? (
                  <textarea
                    id={`f-${f.key}`}
                    className="input min-h-[120px] font-mono text-xs"
                    value={form[f.key] ?? ""}
                    placeholder={f.placeholder}
                    onChange={(e) =>
                      setForm({ ...form, [f.key]: e.target.value })
                    }
                  />
                ) : (
                  <input
                    id={`f-${f.key}`}
                    className="input"
                    type={f.kind === "password" ? "password" : f.kind === "number" ? "number" : "text"}
                    value={form[f.key] ?? ""}
                    placeholder={f.placeholder}
                    autoComplete={f.secret ? "new-password" : "off"}
                    onChange={(e) =>
                      setForm({ ...form, [f.key]: e.target.value })
                    }
                  />
                )}

                {f.help && (
                  <p className="mt-1 text-xs text-muted">{f.help}</p>
                )}
              </div>
            ))}

            {error && (
              <p className="rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-danger">
                {error}
              </p>
            )}

            <div className="flex flex-wrap gap-3">
              <button type="submit" className="btn-primary" disabled={busy}>
                {busy ? "Saving…" : "Save connection"}
              </button>
              <button
                type="button"
                className="btn-ghost"
                onClick={() => setAdding(null)}
                disabled={busy}
              >
                Cancel
              </button>
            </div>
            <p className="text-xs text-muted">
              Saving stores the credentials. It does not check them — use{" "}
              <strong>Test</strong> afterwards to find out whether they work.
            </p>
          </form>
        </div>
      )}
    </div>
  );
}
