// ============================================================================
// Consent History (/user/consent-history)
// Every consent action ever taken, grouped by purpose, filterable, exportable.
// The rows come from the audit trail — the same source the regulator sees.
// ============================================================================
import { useEffect, useMemo, useState } from "react";
import { consentHistoryRows } from "../../api/consent";
import { useApp } from "../../context/AppContext";
import StatusBadge from "../../components/common/StatusBadge";
import AuditHashBadge from "../../components/common/AuditHashBadge";

const ACTION_LABEL = {
  grant: "Given",
  withdraw: "Withdrawn",
  update: "Updated",
  renew: "Renewed",
};

export default function ConsentHistory() {
  const { user } = useApp();
  const [rows, setRows] = useState([]);
  const [purposes, setPurposes] = useState([]);
  const [purpose, setPurpose] = useState("");
  const [status, setStatus] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [q, setQ] = useState("");

  useEffect(() => {
    // Read from the audit chain. Failure leaves the table empty rather than
    // crashing the screen — an empty history is a legitimate state for a new
    // account, and a stack trace would not tell the reader anything useful.
    consentHistoryRows(user)
      .then(({ rows: r, purposes: p }) => {
        setRows(r);
        setPurposes(p);
      })
      .catch(() => setRows([]));
  }, [user]);

  const filtered = useMemo(
    () =>
      rows.filter((r) => {
        if (purpose && r.purpose_id !== purpose) return false;
        if (status && r.consent_status !== status) return false;
        if (from && r.timestamp < new Date(from).toISOString()) return false;
        if (to && r.timestamp > new Date(to + "T23:59:59").toISOString()) return false;
        if (q) {
          const hay = `${r.purpose} ${r.action_type} ${r.log_id} ${r.method}`.toLowerCase();
          if (!hay.includes(q.toLowerCase())) return false;
        }
        return true;
      }),
    [rows, purpose, status, from, to, q]
  );

  // Group by purpose for the timeline view.
  const grouped = useMemo(() => {
    const map = new Map();
    filtered.forEach((r) => {
      const key = r.purpose || r.purpose_id;
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(r);
    });
    return [...map.entries()];
  }, [filtered]);

  // RFC 4180 quoting. Not pedantry: a purpose named "Marketing, offers and
  // partners" previously split into two columns and shifted every field after
  // it, silently, in a file somebody exports precisely because they want a
  // record they can rely on.
  const csvCell = (value) => {
    const text = value === null || value === undefined ? "" : String(value);
    return /[",\n\r]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
  };

  const exportCsv = () => {
    const columns = [
      "log_id", "purpose", "action", "timestamp",
      "method", "version", "consent_status", "hash",
    ];
    const body = [
      columns.join(","),
      ...filtered.map((r) =>
        [
          r.log_id, r.purpose, r.action_type, r.timestamp,
          r.method, r.version, r.consent_status, r.audit_hash,
        ].map(csvCell).join(",")
      ),
    ].join("\r\n");

    const blob = new Blob([body], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "consent-history.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

  // The browser's own print-to-PDF, not a generated file.
  //
  // This button used to produce a .txt with a `.pdf` label swapped onto it,
  // which is the one thing a consent history must not do — hand somebody a file
  // that is not what it says it is. The honest options were a PDF library in
  // the bundle or the printer, and the printer wins: every browser can save the
  // dialogue's output as a real PDF, it renders the table the reader is
  // actually looking at, and it costs no dependency.
  //
  // `@media print` in index.css hides the app chrome and the filter controls.
  const exportPdf = () => window.print();

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink">Consent History</h1>
          <p className="text-sm text-muted">
            Every consent action ever taken on your account, in order.
          </p>
        </div>
        <div className="no-print flex gap-2">
          <button type="button" className="btn-secondary" onClick={exportPdf}>
            Save as PDF
          </button>
          <button type="button" className="btn-secondary" onClick={exportCsv}>
            Export CSV
          </button>
        </div>
      </div>

      <div className="card grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-5">
        <div className="lg:col-span-2">
          <label className="label" htmlFor="q">Search</label>
          <input id="q" className="input" value={q} onChange={(e) => setQ(e.target.value)}
                 placeholder="Purpose, action, log id…" />
        </div>
        <div>
          <label className="label" htmlFor="f-purpose">Purpose</label>
          <select id="f-purpose" className="input" value={purpose} onChange={(e) => setPurpose(e.target.value)}>
            <option value="">All purposes</option>
            {purposes.map((n) => (
              <option key={n.id} value={n.key}>{n.name}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="f-status">Status</label>
          <select id="f-status" className="input" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">Any status</option>
            <option value="active">Active</option>
            <option value="withdrawn">Withdrawn</option>
          </select>
        </div>
        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="label" htmlFor="f-from">From</label>
            <input id="f-from" type="date" className="input" value={from} onChange={(e) => setFrom(e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="f-to">To</label>
            <input id="f-to" type="date" className="input" value={to} onChange={(e) => setTo(e.target.value)} />
          </div>
        </div>
      </div>

      {grouped.length === 0 && (
        <p className="card p-6 text-center text-sm text-muted">No entries match those filters.</p>
      )}

      <div className="space-y-4">
        {grouped.map(([purposeName, entries]) => (
          <section key={purposeName} className="card p-5">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="font-semibold text-ink">{purposeName}</h2>
              <span className="tag">{entries.length} entries</span>
            </div>

            <ol className="mt-4">
              {entries.map((e, i) => (
                <li key={e.id} className="flex gap-3">
                  <div className="flex flex-col items-center">
                    <span
                      className={`mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full ${
                        e.action_type === "withdraw" ? "bg-danger" : "bg-success"
                      }`}
                      aria-hidden="true"
                    />
                    {i < entries.length - 1 && <span className="w-0.5 flex-1 bg-line" aria-hidden="true" />}
                  </div>
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1 pb-5">
                    <span className="text-sm font-medium text-ink">
                      {ACTION_LABEL[e.action_type] || e.action_type}
                    </span>
                    <StatusBadge status={e.consent_status} />
                    <span className="text-xs text-muted">
                      {new Date(e.timestamp).toLocaleString()}
                    </span>
                    <span className="tag">method: {e.method}</span>
                    <span className="tag">v{e.version}</span>
                    <AuditHashBadge hash={e.audit_hash} chars={10} />
                  </div>
                </li>
              ))}
            </ol>
          </section>
        ))}
      </div>
    </div>
  );
}
