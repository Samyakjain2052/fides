// ============================================================================
// DSAR Admin Queue (/admin/dsar)
// The main working screen: filter bar, search, sortable table with the SLA
// countdown, and a slide-in detail panel with the actions for each request type.
// ============================================================================
import { useEffect, useMemo, useState } from "react";
import {
  getAuditLogs,
  getDSARRequests,
} from "../../api";
import { useApp } from "../../context/AppContext";
import StatusBadge from "../../components/common/StatusBadge";
import SLACountdown, { slaTone } from "../../components/common/SLACountdown";
import SlideOver from "../../components/common/SlideOver";
import ConfirmModal from "../../components/common/ConfirmModal";
import AuditHashBadge from "../../components/common/AuditHashBadge";

const FILTERS = ["All", "Access", "Correct", "Erase", "Pending", "In Progress", "Completed", "Overdue"];
const STATUSES = ["pending", "in_progress", "completed", "rejected"];
const SORTS = [
  { id: "deadline_at", label: "Deadline" },
  { id: "submitted_at", label: "Submitted" },
  { id: "reference", label: "Reference" },
  { id: "type", label: "Type" },
  { id: "status", label: "Status" },
];

export default function DSARQueue() {
  const { notify } = useApp();
  const [rows, setRows] = useState([]);
  const [filter, setFilter] = useState("All");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState("deadline_at");
  const [dir, setDir] = useState("asc");
  const [selected, setSelected] = useState(null);
  const [audit, setAudit] = useState([]);
  const [busy, setBusy] = useState(false);

  // panel form state
  const [nextStatus, setNextStatus] = useState("pending");
  const [rejection, setRejection] = useState("");
  const [exempt, setExempt] = useState(false);
  const [exemptReason, setExemptReason] = useState("");
  const [confirmErase, setConfirmErase] = useState(false);

  const load = () => getDSARRequests().then(setRows);

  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    if (!selected) return;
    setNextStatus(selected.status);
    setRejection(selected.rejection_reason || "");
    setExempt(Boolean(selected.exempt));
    setExemptReason(selected.exempt_reason || "");
    getAuditLogs({ user_id: selected.user_id }).then((all) =>
      setAudit(all.filter((a) => a.action_type.startsWith("dsar")))
    );
  }, [selected]);

  const filtered = useMemo(() => {
    let out = [...rows];
    const f = filter.toLowerCase().replace(" ", "_");
    if (["access", "correct", "erase"].includes(f)) out = out.filter((r) => r.type === f);
    else if (["pending", "in_progress", "completed"].includes(f)) out = out.filter((r) => r.status === f);
    else if (f === "overdue")
      out = out.filter((r) => r.status !== "completed" && r.status !== "rejected" && slaTone(r.deadline_at) === "overdue");

    if (q) {
      const needle = q.toLowerCase();
      out = out.filter(
        (r) => r.reference.toLowerCase().includes(needle) || (r.user_email || "").toLowerCase().includes(needle)
      );
    }

    out.sort((a, b) => {
      const av = a[sort] || "";
      const bv = b[sort] || "";
      return dir === "asc" ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av));
    });
    return out;
  }, [rows, filter, q, sort, dir]);

  const toggleSort = (id) => {
    if (sort === id) setDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSort(id);
      setDir("asc");
    }
  };

  const save = async (extra = {}) => {
    setBusy(true);
    try {
      const target = extra.status || nextStatus;
      const updated = await changeStatus(selected.id, {
        toStatus: target,
        // The server AND the database refuse a rejection with no reason, so
        // sending it is not optional — see the error surfaced below.
        reason: target === "rejected" ? rejection : undefined,
        note: exempt ? `Retention exemption applied: ${exemptReason}` : undefined,
      });
      setSelected(updated);
      await load();
      notify(`Request ${updated.reference} is now ${updated.status}.`);
    } catch (e) {
      // The server gives real reasons — an illegal transition names what IS
      // allowed, a reasonless rejection says so. Passing them through is the
      // difference between a usable queue and a mystery.
      notify(e.message || "That change could not be made.", "error");
    } finally {
      setBusy(false);
    }
  };

  const doExport = async () => {
    setBusy(true);
    try {
      // The real access package. Every retrieval is audited server-side, and
      // an expired package says so rather than 404ing.
      await downloadPackage(selected.id, selected.reference);
      await load();
      notify("Access package downloaded. The retrieval is in the audit trail.");
    } catch (e) {
      notify(e.message || "The package could not be retrieved.", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-ink">DSAR queue</h1>
        <p className="text-sm text-muted">
          Every rights request, with its statutory deadline. 30 days from submission.
        </p>
      </div>

      <div className="card space-y-3 p-4">
        <div className="flex flex-wrap gap-2">
          {FILTERS.map((f) => (
            <button
              key={f}
              type="button"
              onClick={() => setFilter(f)}
              className={`rounded-full px-3 py-1.5 text-sm transition ${
                filter === f ? "bg-navy text-white" : "border border-line bg-surface text-ink hover:bg-line/40"
              }`}
            >
              {f}
            </button>
          ))}
        </div>
        <div>
          <label className="sr-only" htmlFor="dsar-search">Search</label>
          <input
            id="dsar-search"
            className="input"
            placeholder="Search by reference number or user email…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
      </div>

      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-line">
            <thead className="bg-canvas">
              <tr>
                {SORTS.map((s) => (
                  <th key={s.id} className="th">
                    <button type="button" className="inline-flex items-center gap-1"
                            onClick={() => toggleSort(s.id)}>
                      {s.label}
                      {sort === s.id && <span aria-hidden="true">{dir === "asc" ? "↑" : "↓"}</span>}
                    </button>
                  </th>
                ))}
                <th className="th">User</th>
                <th className="th">SLA</th>
                <th className="th sr-only">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {filtered.length === 0 && (
                <tr>
                  <td className="td text-center text-muted" colSpan={8}>
                    No requests match this view.
                  </td>
                </tr>
              )}
              {filtered.map((r) => {
                const overdue =
                  r.status !== "completed" && r.status !== "rejected" && slaTone(r.deadline_at) === "overdue";
                return (
                  <tr
                    key={r.id}
                    onClick={() => setSelected(r)}
                    className={`cursor-pointer hover:bg-canvas ${overdue ? "bg-danger/5" : ""}`}
                  >
                    <td className="td font-mono text-xs">{r.reference}</td>
                    <td className="td">
                      <span className="tag capitalize">{r.type}</span>
                    </td>
                    <td className="td text-xs text-muted">
                      {new Date(r.submitted_at).toLocaleDateString()}
                    </td>
                    <td className="td text-xs">
                      {new Date(r.deadline_at).toLocaleDateString()}
                    </td>
                    <td className="td">
                      <StatusBadge status={overdue ? "overdue" : r.status} />
                    </td>
                    <td className="td text-xs text-muted">{r.user_email}</td>
                    <td className="td">
                      {r.status === "completed" || r.status === "rejected" ? (
                        <span className="text-xs text-muted">closed</span>
                      ) : (
                        <SLACountdown deadlineAt={r.deadline_at} />
                      )}
                    </td>
                    <td className="td">
                      <span className="text-sm text-teal underline">View</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* ------------------------------------------------- detail panel -- */}
      <SlideOver
        open={Boolean(selected)}
        title={selected ? `${selected.type.toUpperCase()} — ${selected.reference}` : ""}
        subtitle={selected?.user_email}
        onClose={() => setSelected(null)}
        footer={
          selected && (
            <div className="flex flex-wrap items-center gap-3">
              <button type="button" className="btn-primary" onClick={() => save()} disabled={busy}>
                {busy ? "Saving…" : "Save & Notify User"}
              </button>
              <button type="button" className="btn-ghost" onClick={() => setSelected(null)}>
                Close
              </button>
            </div>
          )
        }
      >
        {selected && (
          <div className="space-y-5">
            <dl className="grid gap-3 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-xs text-muted">Type</dt>
                <dd className="capitalize">{selected.type}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted">Submitted</dt>
                <dd>{new Date(selected.submitted_at).toLocaleString()}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted">Legal deadline</dt>
                <dd>
                  {new Date(selected.deadline_at).toLocaleDateString()}
                  {/* A closed request has no live clock — showing "OVERDUE" against
                      something already answered would misreport compliance. */}
                  <div className="mt-1">
                    {selected.status === "completed" || selected.status === "rejected" ? (
                      <span className="text-xs text-muted">
                        Closed{selected.resolved_at ? ` ${new Date(selected.resolved_at).toLocaleDateString()}` : ""}
                        {selected.resolved_at && new Date(selected.resolved_at) <= new Date(selected.deadline_at)
                          ? " — within deadline"
                          : ""}
                      </span>
                    ) : (
                      <SLACountdown deadlineAt={selected.deadline_at} />
                    )}
                  </div>
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted">Identity verification</dt>
                <dd>
                  <StatusBadge
                    status="verified"
                    label={selected.verification === "digilocker" ? "DigiLocker verified" : "OTP verified"}
                  />
                </dd>
              </div>
            </dl>

            {/* type-specific action */}
            <div className="rounded-lg border border-line p-4">
              <p className="text-sm font-semibold text-ink">Action required</p>

              {selected.type === "access" && (
                <div className="mt-2">
                  <p className="text-sm text-muted">
                    Collect every piece of personal data held about this person and package it.
                  </p>
                  <button type="button" className="btn-secondary mt-3" onClick={doExport} disabled={busy}>
                    Prepare Data Export
                  </button>
                  {selected.export_url && (
                    <p className="mt-2 text-xs text-success">
                      Export ready — <a className="underline" href={selected.export_url}>download</a>
                    </p>
                  )}
                </div>
              )}

              {selected.type === "correct" && (
                <div className="mt-2 space-y-2">
                  {selected.correction ? (
                    <div className="rounded-lg bg-canvas p-3 text-sm">
                      <p className="text-xs text-muted">Requested correction</p>
                      <p className="mt-1 text-ink">
                        <strong>{selected.correction.field}</strong>: “{selected.correction.current}”
                        → “{selected.correction.corrected}”
                      </p>
                    </div>
                  ) : (
                    <p className="text-sm text-muted">No correction details recorded.</p>
                  )}
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={() => {
                      setNextStatus("completed");
                      save({ status: "completed" });
                    }}
                    disabled={busy}
                  >
                    Mark Corrected
                  </button>
                </div>
              )}

              {selected.type === "erase" && (
                <div className="mt-2 space-y-2">
                  <p className="text-sm text-muted">
                    Erasure removes personal data across every connected system. Records held under a
                    legal obligation are retained and reported back to the user.
                  </p>
                  <button type="button" className="btn-danger" onClick={() => setConfirmErase(true)}
                          disabled={busy}>
                    Initiate Erasure
                  </button>
                </div>
              )}
            </div>

            {/* status */}
            <div>
              <label className="label" htmlFor="p-status">Status</label>
              <select id="p-status" className="input" value={nextStatus}
                      onChange={(e) => setNextStatus(e.target.value)}>
                {STATUSES.map((s) => (
                  <option key={s} value={s}>{s.replace("_", " ")}</option>
                ))}
              </select>

              {nextStatus === "rejected" && (
                <div className="mt-3">
                  <label className="label" htmlFor="p-reason">Reason for rejection (shown to the user)</label>
                  <textarea id="p-reason" className="input min-h-[80px]" value={rejection}
                            onChange={(e) => setRejection(e.target.value)} />
                </div>
              )}
            </div>

            {/* legal exception */}
            <div className="rounded-lg border border-line p-4">
              <label className="flex items-start justify-between gap-3">
                <span className="text-sm text-ink">
                  This request is exempt under law
                  <span className="mt-0.5 block text-xs text-muted">
                    e.g. data retained under a statutory obligation. The reason is recorded.
                  </span>
                </span>
                <button
                  type="button"
                  role="switch"
                  aria-checked={exempt}
                  aria-label="Legal exception"
                  onClick={() => setExempt((v) => !v)}
                  className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition ${
                    exempt ? "bg-warning" : "bg-line"
                  }`}
                >
                  <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition ${
                    exempt ? "translate-x-6" : "translate-x-1"
                  }`} />
                </button>
              </label>
              {exempt && (
                <textarea
                  className="input mt-3 min-h-[70px]"
                  placeholder="Which law or rule requires this?"
                  value={exemptReason}
                  onChange={(e) => setExemptReason(e.target.value)}
                />
              )}
            </div>

            {/* audit trail for this request */}
            <div>
              <p className="text-sm font-semibold text-ink">Audit trail for this request</p>
              {audit.length === 0 ? (
                <p className="mt-1 text-sm text-muted">No entries yet.</p>
              ) : (
                <ul className="mt-2 divide-y divide-line">
                  {audit.map((a) => (
                    <li key={a.id} className="flex flex-wrap items-center gap-2 py-2 text-xs">
                      <span className="font-mono">{a.log_id}</span>
                      <span className="text-ink">{a.action_type}</span>
                      <span className="text-muted">{new Date(a.timestamp).toLocaleString()}</span>
                      <span className="text-muted">by {a.initiator}</span>
                      <AuditHashBadge hash={a.audit_hash} chars={10} />
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
      </SlideOver>

      <ConfirmModal
        open={confirmErase}
        title="Initiate erasure?"
        body={`This will erase personal data for ${selected?.user_email} across every connected system.`}
        consequences={[
          "Personal identifiers are removed or masked in each system that holds them.",
          "Records held under a legal obligation are retained, and reported to the user.",
          "The erasure is irreversible.",
          "Every collection touched is written to the audit trail.",
        ]}
        confirmLabel="Yes, initiate erasure"
        busy={busy}
        onCancel={() => setConfirmErase(false)}
        onConfirm={async () => {
          setNextStatus("in_progress");
          await save({ status: "in_progress" });
          setConfirmErase(false);
        }}
      />
    </div>
  );
}
