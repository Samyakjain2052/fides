// ============================================================================
// Grievance Queue (/admin/grievances)
// Also the Grievance Officer's only screen. Filter, table with days-open, and a
// detail panel with resolution notes, escalation, and closure.
// ============================================================================
import { useEffect, useMemo, useState } from "react";
import {
  getDSARRequests,
  getGrievances,
  GRIEVANCE_ESCALATION_DAYS,
  updateGrievance,
} from "../../api";
import { useApp } from "../../context/AppContext";
import StatusBadge from "../../components/common/StatusBadge";
import SlideOver from "../../components/common/SlideOver";
import ConfirmModal from "../../components/common/ConfirmModal";
import { previewLock } from "../../config/modules";

const FILTERS = ["All", "Open", "In Progress", "Resolved", "Escalated"];
const DAY = 864e5;

export default function GrievanceQueue() {
  const { notify, role } = useApp();
  const [rows, setRows] = useState([]);
  const [dsars, setDsars] = useState([]);
  const [filter, setFilter] = useState("All");
  const [selected, setSelected] = useState(null);
  const [notes, setNotes] = useState("");
  const [status, setStatus] = useState("open");
  const [confirmEscalate, setConfirmEscalate] = useState(false);
  const [confirmResolve, setConfirmResolve] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = () => getGrievances().then(setRows);

  useEffect(() => {
    load();
    getDSARRequests().then(setDsars);
  }, []);

  useEffect(() => {
    if (!selected) return;
    setNotes(selected.resolution_notes || "");
    setStatus(selected.status);
  }, [selected]);

  const filtered = useMemo(() => {
    const f = filter.toLowerCase().replace(" ", "_");
    if (f === "all") return rows;
    if (f === "escalated") return rows.filter((g) => g.escalated);
    return rows.filter((g) => g.status === f);
  }, [rows, filter]);

  const daysOpen = (g) => Math.floor((Date.now() - new Date(g.submitted_at)) / DAY);

  const save = async (patch, message) => {
    setBusy(true);
    try {
      const res = await updateGrievance(selected.id, { resolution_notes: notes, notify: true, ...patch });
      setSelected(res.grievance);
      await load();
      notify(message || `Complaint ${res.grievance.reference} updated and the user notified.`);
    } finally {
      setBusy(false);
      setConfirmEscalate(false);
      setConfirmResolve(false);
    }
  };

  const linkedDsar = selected?.related_dsar
    ? dsars.find((d) => d.id === selected.related_dsar || d.reference === selected.related_dsar)
    : null;

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-ink">Grievance queue</h1>
        <p className="text-sm text-muted">
          Complaints escalate automatically after {GRIEVANCE_ESCALATION_DAYS} days unresolved.
          {role === "grievance_officer" && " You can update resolution status here."}
        </p>
      </div>

      <div className="card p-4">
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
      </div>

      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-line">
            <thead className="bg-canvas">
              <tr>
                <th className="th">Reference</th>
                <th className="th">User</th>
                <th className="th">Category</th>
                <th className="th">Days open</th>
                <th className="th">Status</th>
                <th className="th">Officer</th>
                <th className="th sr-only">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {filtered.length === 0 && (
                <tr><td className="td text-center text-muted" colSpan={7}>Nothing in this view.</td></tr>
              )}
              {filtered.map((g) => {
                const stale = g.status !== "resolved" && daysOpen(g) > GRIEVANCE_ESCALATION_DAYS;
                return (
                  <tr key={g.id} onClick={() => setSelected(g)}
                      className={`cursor-pointer hover:bg-canvas ${stale ? "bg-danger/5" : ""}`}>
                    <td className="td font-mono text-xs">{g.reference}</td>
                    <td className="td text-xs text-muted">{g.user_email}</td>
                    <td className="td">{g.category}</td>
                    <td className={`td ${stale ? "font-semibold text-danger" : ""}`}>
                      {daysOpen(g)}
                      {stale && " — past threshold"}
                    </td>
                    <td className="td"><StatusBadge status={g.escalated ? "escalated" : g.status} /></td>
                    <td className="td text-xs text-muted">{g.officer}</td>
                    <td className="td"><span className="text-sm text-teal underline">View</span></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <SlideOver
        open={Boolean(selected)}
        title={selected ? `${selected.category} — ${selected.reference}` : ""}
        subtitle={selected?.user_email}
        onClose={() => setSelected(null)}
        footer={
          selected && (
            <div className="flex flex-wrap items-center gap-3">
              <button type="button" className="btn-primary" onClick={() => save({ status })} disabled={busy} {...previewLock("grievance", "Updating a grievance")}>
                {busy ? "Saving…" : "Save & Notify User"}
              </button>
              {!selected.escalated && selected.status !== "resolved" && (
                <button type="button" className="btn-secondary" onClick={() => setConfirmEscalate(true)} {...previewLock("grievance", "Escalating a grievance")}>
                  Escalate to DPO
                </button>
              )}
              {selected.status !== "resolved" && (
                <button type="button" className="btn-secondary" onClick={() => setConfirmResolve(true)} {...previewLock("grievance", "Resolving a grievance")}>
                  Mark Resolved
                </button>
              )}
            </div>
          )
        }
      >
        {selected && (
          <div className="space-y-5">
            <div className="flex flex-wrap items-center gap-3">
              <StatusBadge status={selected.escalated ? "escalated" : selected.status} />
              <span className="text-sm text-muted">
                {daysOpen(selected)} days open · filed{" "}
                {new Date(selected.submitted_at).toLocaleDateString()}
              </span>
            </div>

            <div>
              <p className="text-xs text-muted">Complaint</p>
              <p className="mt-1 rounded-lg border border-line bg-canvas p-3 text-sm text-ink">
                {selected.description}
              </p>
            </div>

            <div className="grid gap-3 text-sm sm:grid-cols-2">
              <div>
                <p className="text-xs text-muted">Related data request</p>
                {linkedDsar ? (
                  <p className="mt-1">
                    <span className="font-mono text-xs">{linkedDsar.reference}</span>{" "}
                    <span className="capitalize text-muted">({linkedDsar.type})</span>
                  </p>
                ) : (
                  <p className="mt-1 text-muted">None</p>
                )}
              </div>
              <div>
                <p className="text-xs text-muted">Assigned officer</p>
                <p className="mt-1">{selected.officer}</p>
              </div>
            </div>

            {selected.escalated && (
              <div className="rounded-lg border border-danger/40 bg-danger/5 p-3 text-sm">
                <p className="font-medium text-ink">Escalated to the DPO</p>
                <p className="mt-1 text-muted">
                  The Data Protection Officer has been notified and the escalation is in the audit
                  trail.
                </p>
              </div>
            )}

            <div>
              <label className="label" htmlFor="g-status">Status</label>
              <select id="g-status" className="input" value={status}
                      onChange={(e) => setStatus(e.target.value)}>
                {["open", "acknowledged", "in_progress", "resolved"].map((s) => (
                  <option key={s} value={s}>{s.replace("_", " ")}</option>
                ))}
              </select>
            </div>

            <div>
              <label className="label" htmlFor="g-notes">Resolution notes (shown to the user)</label>
              <textarea id="g-notes" className="input min-h-[110px]" value={notes}
                        onChange={(e) => setNotes(e.target.value)}
                        placeholder="What did you find, and what did you do about it?" />
            </div>

            {selected.feedback && (
              <div className="rounded-lg border border-line p-3 text-sm">
                <p className="text-xs text-muted">User feedback on the resolution</p>
                <p className="mt-1 text-warning">
                  {"★".repeat(selected.feedback.rating)}
                  <span className="text-line">{"★".repeat(5 - selected.feedback.rating)}</span>
                </p>
                {selected.feedback.comment && (
                  <p className="mt-1 text-muted">{selected.feedback.comment}</p>
                )}
              </div>
            )}
          </div>
        )}
      </SlideOver>

      <ConfirmModal
        open={confirmEscalate}
        destructive={false}
        title="Escalate to the Data Protection Officer?"
        body={`Complaint ${selected?.reference} has been open ${selected ? daysOpen(selected) : 0} days.`}
        consequences={[
          "The DPO is notified immediately.",
          "The escalation is written to the audit trail and cannot be removed.",
          "The user is told their complaint has been escalated.",
        ]}
        confirmLabel="Escalate"
        busy={busy}
        onCancel={() => setConfirmEscalate(false)}
        onConfirm={() => save({ escalated: true, status: "in_progress" }, "Escalated to the DPO.")}
      />

      <ConfirmModal
        open={confirmResolve}
        destructive={false}
        title="Mark this complaint resolved?"
        body="The user will be notified and invited to rate the resolution."
        consequences={[
          "The resolution notes above are sent to the user.",
          "The closure is recorded in the audit trail.",
          "The user can leave feedback on how it was handled.",
        ]}
        confirmLabel="Mark resolved"
        busy={busy}
        onCancel={() => setConfirmResolve(false)}
        onConfirm={() => save({ status: "resolved" }, "Complaint resolved and the user notified.")}
      />
    </div>
  );
}
