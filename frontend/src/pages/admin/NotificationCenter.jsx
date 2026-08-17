// ============================================================================
// Notification Center (/admin/notifications)
//
// Two things: the delivery log, and the template editor. Both against the real
// API.
//
// What this screen deliberately no longer has:
//
//   * A "Fiduciary / Processor Alerts" tab. It showed webhook HTTP codes,
//     acknowledgements and escalations for a processor-alerting system that does
//     not exist. Every field in it was invented.
//   * A "Send Test Alert" button, which posted nothing anywhere and then said
//     "delivered (HTTP 200)".
//
// What it gained: the provider banner. If the console provider is configured,
// nothing is actually being emailed, and a screen full of green "Delivered" rows
// that doesn't say so is the single most misleading thing this product could
// show a compliance officer.
// ============================================================================
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  deliveryLog,
  drain,
  getProvider,
  listTemplates,
  previewTemplate,
  retry as retryNotification,
  saveTemplate,
} from "../../api/notifications";
import { LANGUAGES } from "../../api";
import { useApp } from "../../context/AppContext";
import StatusBadge from "../../components/common/StatusBadge";

/** Mirrors TEMPLATE_KEYS on the server. The server is the authority; this is labels. */
const KEY_LABELS = {
  "dsar.received": "Data request received",
  "dsar.completed": "Data request completed",
  "dsar.rejected": "Data request refused",
  "consent.withdrawn": "Consent withdrawn",
  "grievance.received": "Grievance received",
  "grievance.escalated": "Grievance escalated",
  "grievance.resolved": "Grievance resolved",
  "retention.pre_purge": "Data scheduled for deletion",
};

const STATUS_FILTERS = [
  { id: "", label: "All" },
  { id: "delivered", label: "Delivered" },
  { id: "queued", label: "Queued" },
  { id: "failed", label: "Failed" },
  { id: "suppressed", label: "Not sent" },
];

function fmt(iso) {
  return iso ? new Date(iso).toLocaleString() : "—";
}

export default function NotificationCenter() {
  const { notify } = useApp();

  const [provider, setProvider] = useState(null);
  const [rows, setRows] = useState([]);
  const [status, setStatus] = useState("");
  const [templates, setTemplates] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  // editor
  const [key, setKey] = useState("dsar.received");
  const [language, setLanguage] = useState("English");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [rendered, setRendered] = useState(null);

  const loadLog = useCallback(async () => {
    try {
      setRows(await deliveryLog({ status: status || undefined }));
    } catch (e) {
      setError(e.message);
    }
  }, [status]);

  useEffect(() => {
    getProvider().then(setProvider).catch((e) => setError(e.message));
    listTemplates().then(setTemplates).catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    loadLog();
  }, [loadLog]);

  // Load whichever template matches the current (key, language) selection. An
  // absent one leaves the fields empty rather than showing another language's
  // text under this language's label.
  useEffect(() => {
    const match = templates.find(
      (t) => t.key === key && t.language === language && t.channel === "email",
    );
    setSubject(match?.subject ?? "");
    setBody(match?.body ?? "");
    setRendered(null);
  }, [key, language, templates]);

  const existing = useMemo(
    () => templates.find((t) => t.key === key && t.language === language),
    [templates, key, language],
  );

  const counts = useMemo(() => {
    const out = { delivered: 0, queued: 0, failed: 0, suppressed: 0 };
    for (const r of rows) if (r.status in out) out[r.status] += 1;
    return out;
  }, [rows]);

  const act = async (fn, message) => {
    setBusy(true);
    setError(null);
    try {
      const result = await fn();
      if (message) notify(typeof message === "function" ? message(result) : message);
      return result;
    } catch (e) {
      setError(e.message);
      return null;
    } finally {
      setBusy(false);
    }
  };

  const onPreview = () =>
    act(async () => {
      const out = await previewTemplate({ key, language, subject, body });
      setRendered(out);
      return out;
    });

  const onSave = () =>
    act(async () => {
      await saveTemplate({ key, language, subject, body });
      setTemplates(await listTemplates());
    }, `Template saved for ${language}.`);

  const onRetry = (id) =>
    act(async () => {
      const row = await retryNotification(id);
      await loadLog();
      return row;
    }, (row) => (row?.status === "delivered" ? "Delivered." : "Attempted — see the log."));

  const onDrain = () =>
    act(async () => {
      const result = await drain();
      await loadLog();
      return result;
    }, (r) =>
      r?.claimed
        ? `${r.claimed} claimed — ${r.delivered} delivered, ${r.failed} failed, ${r.requeued} requeued.`
        : "Nothing was due.");

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-ink">Notification centre</h1>
        <p className="text-sm text-muted">
          Every message this platform sent, why it was sent, and what it says.
        </p>
      </div>

      {/* --------------------------------------------------- provider state -- */}
      {provider && !provider.sends_real_messages && (
        <div className="rounded-lg border border-warning/50 bg-warning/10 p-4 text-sm">
          <p className="font-semibold text-ink">
            No email is actually being sent.
          </p>
          <p className="mt-1 text-muted">
            The <span className="font-mono">{provider.name}</span> provider is
            configured, which writes each message to the server log instead of
            delivering it. Rows below marked <em>Delivered</em> mean the provider
            accepted them — not that anyone received them. Set{" "}
            <span className="font-mono">DS_NOTIFICATION_PROVIDER</span> and its
            credentials to send for real.
          </p>
        </div>
      )}
      {provider?.sends_real_messages && (
        <p className="text-xs text-muted">
          Sending via <span className="font-mono">{provider.name}</span>
          {provider.from_address ? ` as ${provider.from_address}` : ""}.
        </p>
      )}

      {error && (
        <div className="rounded-lg border border-danger/50 bg-danger/10 p-3 text-sm text-ink">
          {error}
        </div>
      )}

      {/* ------------------------------------------------------ delivery log -- */}
      <section className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          {STATUS_FILTERS.map((f) => (
            <button
              key={f.id || "all"}
              type="button"
              onClick={() => setStatus(f.id)}
              className={`rounded-full px-4 py-1.5 text-sm transition ${
                status === f.id
                  ? "bg-navy text-white"
                  : "border border-line bg-surface text-ink hover:bg-line/40"
              }`}
            >
              {f.label}
            </button>
          ))}
          <button
            type="button"
            className="btn-secondary ml-auto"
            onClick={onDrain}
            disabled={busy}
            title="Attempt every message that is due now. No scheduler is deployed yet, so this is how a queued message gets its next try."
          >
            Process queue now
          </button>
        </div>

        {rows.length > 0 && (
          <p className="text-xs text-muted">
            {counts.delivered} delivered · {counts.queued} queued ·{" "}
            {counts.failed} failed · {counts.suppressed} not sent
          </p>
        )}

        <div className="card overflow-hidden">
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-line">
              <thead className="bg-canvas">
                <tr>
                  <th className="th">Recipient</th>
                  <th className="th">Subject</th>
                  <th className="th">Because</th>
                  <th className="th">Language</th>
                  <th className="th">Status</th>
                  <th className="th">Attempts</th>
                  <th className="th">Queued</th>
                  <th className="th sr-only">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {rows.length === 0 && (
                  <tr>
                    <td className="td text-center text-muted" colSpan={8}>
                      Nothing sent yet. Messages appear here when a request is
                      received or closed, a consent is withdrawn, or data is
                      scheduled for deletion.
                    </td>
                  </tr>
                )}
                {rows.map((n) => (
                  <tr key={n.id}>
                    <td className="td text-xs">{n.to_address}</td>
                    <td className="td">{n.subject_rendered}</td>
                    <td className="td">
                      <span className="tag">{KEY_LABELS[n.template_key] || n.template_key}</span>
                    </td>
                    <td className="td text-xs">
                      {n.language}
                      {/* The fallback, made visible. "We notified them in their
                          language" is a claim somebody will check. */}
                      {n.language_requested && (
                        <span className="ml-1 text-warning">
                          (asked for {n.language_requested})
                        </span>
                      )}
                    </td>
                    <td className="td">
                      <StatusBadge status={n.status} />
                      {n.suppression_reason && (
                        <p className="mt-1 max-w-xs text-xs text-muted">
                          {n.suppression_reason}
                        </p>
                      )}
                      {n.last_error && n.status !== "suppressed" && (
                        <p className="mt-1 max-w-xs text-xs text-danger">{n.last_error}</p>
                      )}
                    </td>
                    <td className="td text-xs">{n.attempts}</td>
                    <td className="td text-xs text-muted">{fmt(n.queued_at)}</td>
                    <td className="td">
                      {n.status === "failed" && (
                        <button
                          type="button"
                          className="text-sm text-teal underline"
                          onClick={() => onRetry(n.id)}
                          disabled={busy}
                        >
                          Retry
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        <p className="text-xs text-muted">
          Message bodies are not stored. The address, the subject and the outcome
          are kept as evidence that a notice was given; keeping the bodies would
          be a second copy of everyone&rsquo;s personal data with its own
          retention problem.
        </p>
      </section>

      {/* -------------------------------------------------- template editor -- */}
      <section className="card p-5">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="font-semibold text-ink">Notification templates</h2>
            <p className="text-xs text-muted">
              One per scenario, per language. Placeholders like{" "}
              <span className="font-mono">{"{{deadline}}"}</span> are filled at
              send time, and a placeholder that would never be supplied is
              refused when you save — not discovered because a statutory notice
              went out with a blank in it.
            </p>
          </div>
          <div className="flex flex-wrap gap-3">
            <div>
              <label className="label" htmlFor="t-key">Scenario</label>
              <select
                id="t-key"
                className="input"
                value={key}
                onChange={(e) => setKey(e.target.value)}
              >
                {(provider?.keys || Object.keys(KEY_LABELS)).map((k) => (
                  <option key={k} value={k}>{KEY_LABELS[k] || k}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="label" htmlFor="t-lang">Language</label>
              <select
                id="t-lang"
                className="input"
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
              >
                {LANGUAGES.map((l) => <option key={l} value={l}>{l}</option>)}
              </select>
            </div>
          </div>
        </div>

        <div className="mt-4 space-y-3">
          {!existing && (
            <p className="rounded-lg border border-line bg-canvas p-3 text-xs text-muted">
              There is no {language} template for this scenario yet. Until one
              exists, messages for it are sent in English and the fallback is
              recorded on every row.
            </p>
          )}
          <div>
            <label className="label" htmlFor="t-subject">Subject</label>
            <input
              id="t-subject"
              className="input"
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
            />
          </div>
          <div>
            <label className="label" htmlFor="t-body">Body</label>
            <textarea
              id="t-body"
              className="input min-h-[150px]"
              value={body}
              onChange={(e) => setBody(e.target.value)}
            />
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <button type="button" className="btn-secondary" onClick={onPreview} disabled={busy}>
              Preview
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={onSave}
              disabled={busy || !subject.trim() || !body.trim()}
            >
              Save template
            </button>
            {existing && (
              <span className="text-xs text-muted">
                Last changed {fmt(existing.updated_at)}.
              </span>
            )}
          </div>

          {rendered && (
            <div className="rounded-lg border border-line bg-canvas p-4">
              <p className="text-xs font-semibold uppercase tracking-wide text-muted">
                Preview — sample values, nothing sent
              </p>
              <p className="mt-2 font-semibold text-ink">{rendered.subject}</p>
              <p className="mt-2 whitespace-pre-wrap text-sm text-ink">{rendered.body}</p>
              <p className="mt-3 text-xs text-muted">
                Available here:{" "}
                <span className="font-mono">
                  {rendered.placeholders_available.map((p) => `{{${p}}}`).join(" ")}
                </span>
              </p>
              {rendered.placeholders_available.filter(
                (p) => !rendered.placeholders_used.includes(p),
              ).length > 0 && (
                <p className="mt-1 text-xs text-muted">
                  Unused:{" "}
                  <span className="font-mono">
                    {rendered.placeholders_available
                      .filter((p) => !rendered.placeholders_used.includes(p))
                      .map((p) => `{{${p}}}`)
                      .join(" ")}
                  </span>
                </p>
              )}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
