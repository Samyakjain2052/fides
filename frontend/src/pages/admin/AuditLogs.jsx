// ============================================================================
// Audit Logs (/admin/audit) — READ ONLY, and real as of this change.
//
// There is deliberately no edit or delete control anywhere on this screen. That
// absence is the feature: an audit trail you can change is not evidence. The
// backend enforces it too — the application's database role holds no UPDATE or
// DELETE grant on the table, and a trigger raises if anything tries.
//
// Rows come from the HMAC-SHA256 hash-chained trail in PostgreSQL. Two things
// this screen previously got wrong, both fixed here, because this is the screen a
// regulator actually reads:
//
//   * The export appended a "digital signature" computed from `rowCount * 7919`.
//     It looked like a hash and proved nothing. It now carries the chain's real
//     head hash and the verification result.
//   * The integrity panel said "Integrity check passed" unconditionally. A
//     tamper-evident trail whose UI cannot display tampering is not
//     tamper-evident to the person looking at it.
// ============================================================================
import { useCallback, useEffect, useMemo, useState } from "react";
import { auditTrail, verifyChain } from "../../api/audit";
import { useApp } from "../../context/AppContext";
import StatusBadge from "../../components/common/StatusBadge";
import AuditHashBadge from "../../components/common/AuditHashBadge";

export default function AuditLogs() {
  const { notify } = useApp();
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [cursor, setCursor] = useState(null);
  const [actions, setActions] = useState([]);
  const [initiators, setInitiators] = useState([]);
  const [filters, setFilters] = useState({
    action_type: "", user_id: "", purpose_id: "", initiator: "", from: "", to: "",
  });
  const [integrity, setIntegrity] = useState(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const t = await auditTrail({ limit: 200 });
      setRows(t.rows);
      setTotal(t.total);
      setCursor(t.nextCursor);
      setActions(t.actions);
      setInitiators(t.initiators);
      setError(null);
    } catch (e) {
      setError(e.message || "Could not load the audit trail.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const loadMore = async () => {
    if (!cursor) return;
    setBusy(true);
    try {
      const t = await auditTrail({ limit: 200, beforeSeq: cursor });
      setRows((prev) => [...prev, ...t.rows]);
      setCursor(t.nextCursor);
    } finally {
      setBusy(false);
    }
  };

  const filtered = useMemo(
    () =>
      rows.filter((r) => {
        if (filters.action_type && r.action_type !== filters.action_type) return false;
        if (filters.user_id &&
            !String(r.user_id).toLowerCase().includes(filters.user_id.toLowerCase())) return false;
        if (filters.purpose_id && r.purpose_id !== filters.purpose_id) return false;
        if (filters.initiator && r.initiator !== filters.initiator) return false;
        if (filters.from && r.timestamp < new Date(filters.from).toISOString()) return false;
        if (filters.to && r.timestamp > new Date(filters.to + "T23:59:59").toISOString()) return false;
        return true;
      }),
    [rows, filters]
  );

  const purposeKeys = useMemo(
    () => [...new Set(rows.map((r) => r.purpose_id).filter(Boolean))].sort(),
    [rows]
  );

  const verify = async () => {
    setBusy(true);
    try {
      const res = await verifyChain();
      setIntegrity(res);
      if (res.ok) {
        notify(`Chain intact across ${res.checked} entries.`);
      } else {
        // Loud, and named. A break here is the most serious thing this product
        // can report; it must not read like a warning about something cosmetic.
        notify(
          `INTEGRITY FAILURE at entry ${res.first_broken_seq}. The audit trail has been altered.`,
          "error"
        );
      }
    } catch (e) {
      notify(e.message || "Could not run the integrity check.", "error");
    } finally {
      setBusy(false);
    }
  };

  const exportRows = async () => {
    // Verify at export time, so the file states a freshly checked fact rather
    // than whatever the last click left on screen.
    let status = null;
    try {
      status = await verifyChain();
      setIntegrity(status);
    } catch {
      status = null;
    }

    const header =
      "seq,timestamp,actor,actor_type,action,entity,purpose_key,source_ip,prev_hash,hash";
    const lines = filtered.map((r) =>
      [r.seq, r.timestamp, r.user_id, r.initiator, r.raw_action, r.entity,
       r.purpose_id, r.source_ip, r.prev_hash, r.audit_hash].join(",")
    );

    // The real chain head, not a number shaped like one. Anyone holding this
    // file can re-run POST /v1/audit/verify and compare.
    const footer = [
      "",
      `# exported_at: ${new Date().toISOString()}`,
      `# rows_in_export: ${filtered.length} of ${total} total entries`,
      status
        ? `# chain_verified: ${status.ok ? "OK" : "FAILED"}  checked: ${status.checked}  head_seq: ${status.head_seq}`
        : "# chain_verified: NOT CHECKED (the verification call failed)",
      status?.head_hash ? `# chain_head_hash: ${status.head_hash}` : "",
      status && !status.ok ? `# first_broken_seq: ${status.first_broken_seq}` : "",
      "# Every row carries prev_hash and hash. Re-run POST /v1/audit/verify to",
      "# confirm this chain independently of this file.",
    ]
      .filter(Boolean)
      .join("\n");

    const blob = new Blob([[header, ...lines].join("\n") + "\n" + footer], {
      type: "text/csv",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `audit-trail-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
    notify(`Exported ${filtered.length} entries with the chain head hash.`);
  };

  if (loading) return <p className="text-sm text-muted">Loading the audit trail…</p>;

  if (error) {
    return (
      <div className="card border-danger/40 bg-danger/5 p-5">
        <p className="font-medium text-ink">Could not load the audit trail</p>
        <p className="mt-1 text-sm text-muted">{error}</p>
        <button type="button" className="btn-secondary mt-4" onClick={load}>Try again</button>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div className="card border-navy/30 bg-navy/5 p-4">
        <p className="flex items-center gap-2 font-semibold text-navy">
          <span aria-hidden="true">🔒</span>
          Immutable audit trail — no edits or deletions permitted
        </p>
        <p className="mt-1 text-sm text-muted">
          Every entry is an HMAC-SHA256 hash over its own contents plus the previous
          entry's hash. Editing one breaks its own hash; removing one breaks the next
          entry's link. The application's database role holds no UPDATE or DELETE
          grant on this table, and a trigger rejects either.
        </p>
      </div>

      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink">Audit logs</h1>
          <p className="text-sm text-muted">
            {filtered.length} of {rows.length} loaded · {total} total entries
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" className="btn-secondary" onClick={verify} disabled={busy}>
            {busy ? "Verifying…" : "Verify chain integrity"}
          </button>
          <button type="button" className="btn-secondary" onClick={exportRows}>
            Export CSV
          </button>
        </div>
      </div>

      {integrity && (
        <div
          role="status"
          className={`card p-4 text-sm ${
            integrity.ok ? "border-success/40 bg-success/5" : "border-danger/60 bg-danger/10"
          }`}
        >
          <p className="flex items-center gap-2 font-semibold text-ink">
            <span
              className={`h-2 w-2 rounded-full ${integrity.ok ? "bg-success" : "bg-danger"}`}
              aria-hidden="true"
            />
            {integrity.ok
              ? "Chain intact"
              : `INTEGRITY FAILURE — the trail has been altered at entry ${integrity.first_broken_seq}`}
          </p>
          <p className="mt-1 text-muted">
            {integrity.checked} {integrity.checked === 1 ? "entry" : "entries"} walked
            {integrity.head_seq != null && <> · head at seq {integrity.head_seq}</>} ·{" "}
            {new Date(integrity.verified_at).toLocaleString()}
          </p>
          {integrity.head_hash && (
            <p className="mt-2 break-all font-mono text-xs text-muted">
              head hash {integrity.head_hash}
            </p>
          )}
          {!integrity.ok && integrity.problem && (
            <p className="mt-2 text-danger">{integrity.problem}</p>
          )}
          {integrity.ok && (
            // Do not let a green tick imply more than it proves.
            <p className="mt-2 text-xs text-muted">
              This proves no entry was edited, removed or reordered. It cannot detect
              removal of the most recent entries — external anchoring covers that and
              is not yet in place.
            </p>
          )}
        </div>
      )}

      <div className="card grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-6">
        <div>
          <label className="label" htmlFor="a-action">Action</label>
          <select id="a-action" className="input" value={filters.action_type}
                  onChange={(e) => setFilters({ ...filters, action_type: e.target.value })}>
            <option value="">All actions</option>
            {actions.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="a-actor">Actor</label>
          <input id="a-actor" className="input" value={filters.user_id}
                 placeholder="email or id"
                 onChange={(e) => setFilters({ ...filters, user_id: e.target.value })} />
        </div>
        <div>
          <label className="label" htmlFor="a-purpose">Purpose</label>
          <select id="a-purpose" className="input" value={filters.purpose_id}
                  onChange={(e) => setFilters({ ...filters, purpose_id: e.target.value })}>
            <option value="">All purposes</option>
            {purposeKeys.map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="a-initiator">Initiator</label>
          <select id="a-initiator" className="input" value={filters.initiator}
                  onChange={(e) => setFilters({ ...filters, initiator: e.target.value })}>
            <option value="">Anyone</option>
            {initiators.map((i) => <option key={i} value={i}>{i}</option>)}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="a-from">From</label>
          <input id="a-from" type="date" className="input" value={filters.from}
                 onChange={(e) => setFilters({ ...filters, from: e.target.value })} />
        </div>
        <div>
          <label className="label" htmlFor="a-to">To</label>
          <input id="a-to" type="date" className="input" value={filters.to}
                 onChange={(e) => setFilters({ ...filters, to: e.target.value })} />
        </div>
      </div>

      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-line">
            <thead className="bg-canvas">
              <tr>
                <th className="th">Seq</th>
                <th className="th">Timestamp</th>
                <th className="th">Actor</th>
                <th className="th">Purpose</th>
                <th className="th">Action</th>
                <th className="th">Consent status</th>
                <th className="th">Initiator</th>
                <th className="th">Source IP</th>
                <th className="th">Audit hash</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {filtered.length === 0 && (
                <tr>
                  <td className="td text-center text-muted" colSpan={9}>
                    No entries match those filters.
                  </td>
                </tr>
              )}
              {filtered.map((r) => (
                <tr key={r.id}>
                  <td className="td font-mono text-xs">{r.log_id}</td>
                  <td className="td text-xs text-muted">{new Date(r.timestamp).toLocaleString()}</td>
                  <td className="td font-mono text-xs">{r.user_id}</td>
                  <td className="td font-mono text-xs">{r.purpose_id || "—"}</td>
                  <td className="td"><span className="tag">{r.action_type}</span></td>
                  <td className="td">
                    {["active", "withdrawn", "expired"].includes(r.consent_status)
                      ? <StatusBadge status={r.consent_status} />
                      : <span className="text-xs text-muted">—</span>}
                  </td>
                  <td className="td text-xs">{r.initiator}</td>
                  <td className="td font-mono text-xs text-muted">{r.source_ip}</td>
                  <td className="td"><AuditHashBadge hash={r.audit_hash} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {cursor && (
          <div className="border-t border-line p-3 text-center">
            <button type="button" className="btn-secondary" onClick={loadMore} disabled={busy}>
              {busy ? "Loading…" : "Load older entries"}
            </button>
          </div>
        )}
      </div>

      <p className="text-xs text-muted">
        Exports carry every row's <span className="font-mono">prev_hash</span> and{" "}
        <span className="font-mono">hash</span>, plus the chain head hash and the
        verification result at export time — so the file can be checked against a
        live <span className="font-mono">POST /v1/audit/verify</span> rather than
        taken on trust. There is no separate signing key yet; that is part of the
        external-anchoring work.
      </p>
    </div>
  );
}
