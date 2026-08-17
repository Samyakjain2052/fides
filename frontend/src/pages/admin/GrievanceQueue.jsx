// ============================================================================
// Grievance Queue (/admin/grievances) — also the Grievance Officer's only screen.
//
// Real triage against /v1/grievances.
//
// Ordered by deadline, not by date filed: the queue's order should be the order
// the statutory risk arrives in. The counts strip is the headline a DPO needs
// before anything else — how many are overdue, how many escalated, and how many
// anonymous filings are sitting unconfirmed.
//
// Two states this screen surfaces that the previous mock had no concept of:
//
//   * **Awaiting confirmation** — filed publicly, address unconfirmed. Recorded
//     and counted, but it will not escalate. Visible so a DPO can see they are
//     being spammed, or that confirmation emails are not arriving.
//   * **Confirmation expired** — the window has closed, so it will never be
//     confirmed and never escalate. Separated because it needs a human, not
//     patience.
//
// Resolution requires notes and rejection requires a reason. The server refuses
// otherwise and so does the database; the button is disabled to match rather than
// to substitute.
// ============================================================================
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  assignGrievance,
  CATEGORY_LABEL,
  changeStatus,
  escalateGrievance,
  getGrievance,
  listGrievances,
  officer as fetchOfficer,
  STATUS_LABEL,
} from "../../api/grievances";
import { listUsers } from "../../api/auth";
import { useApp } from "../../context/AppContext";
import StatusBadge from "../../components/common/StatusBadge";
import SlideOver from "../../components/common/SlideOver";
import ConfirmModal from "../../components/common/ConfirmModal";

const FILTERS = [
  { id: "all", label: "All" },
  { id: "open", label: "Open" },
  { id: "in_progress", label: "In progress" },
  { id: "overdue", label: "Overdue" },
  { id: "escalated", label: "Escalated" },
  { id: "resolved", label: "Resolved" },
];

/** What each status may legally become. Mirrors ALLOWED_TRANSITIONS server-side. */
const NEXT = {
  open: ["acknowledged", "in_progress", "rejected"],
  acknowledged: ["in_progress", "resolved", "rejected"],
  in_progress: ["resolved", "rejected"],
  resolved: [],
  reopened: ["in_progress", "resolved", "rejected"],
  rejected: [],
};

export default function GrievanceQueue() {
  const { notify } = useApp();
  const [rows, setRows] = useState([]);
  const [counts, setCounts] = useState({});
  const [users, setUsers] = useState([]);
  const [officer, setOfficer] = useState(null);
  const [filter, setFilter] = useState("all");
  const [selected, setSelected] = useState(null);
  const [notes, setNotes] = useState("");
  const [reason, setReason] = useState("");
  const [confirmEscalate, setConfirmEscalate] = useState(false);
  const [confirmReject, setConfirmReject] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const q =
        filter === "all"
          ? {}
          : filter === "overdue"
            ? { overdueOnly: true }
            : filter === "escalated"
              ? { escalatedOnly: true }
              : { status: filter };
      const page = await listGrievances(q);
      setRows(page.items);
      setCounts(page.counts);
    } catch (e) {
      setError(e.message);
    }
  }, [filter]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    fetchOfficer().then(setOfficer).catch(() => setOfficer(null));
    // A Grievance Officer cannot list users — that needs USER_MANAGE, and the
    // whole point of the role is that it cannot read anything else. Failing
    // quietly leaves the assignment control empty rather than showing them an
    // error for a permission they are not supposed to have.
    listUsers()
      .then(setUsers)
      .catch(() => setUsers([]));
  }, []);

  useEffect(() => {
    if (!selected) return;
    setNotes(selected.resolution_notes || "");
    setReason(selected.rejection_reason || "");
  }, [selected]);

  const refreshSelected = async (id) => {
    setSelected(await getGrievance(id));
    await load();
  };

  const act = async (fn, message) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      if (selected) await refreshSelected(selected.id);
      if (message) notify(message);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
      setConfirmEscalate(false);
      setConfirmReject(false);
    }
  };

  const move = (toStatus) =>
    act(
      () =>
        changeStatus(selected.id, {
          toStatus,
          resolutionNotes: toStatus === "resolved" ? notes : null,
          rejectionReason: toStatus === "rejected" ? reason : null,
        }),
      toStatus === "resolved"
        ? "Resolved. The person has been emailed."
        : toStatus === "rejected"
          ? "Recorded as not upheld. The person has been emailed the reason."
          : `Moved to ${STATUS_LABEL[toStatus] || toStatus}.`,
    );

  const assignedName = (id) =>
    users.find((u) => u.id === id)?.email || (id ? "assigned" : "unassigned");

  const attention = useMemo(
    () => (counts.overdue || 0) + (counts.escalated || 0),
    [counts],
  );

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-ink">Grievance queue</h1>
        <p className="text-sm text-muted">
          DPDP §13 complaints, soonest deadline first.
          {officer &&
            ` Unresolved complaints escalate to the Grievance Officer after ${officer.escalation_days} days.`}
        </p>
      </div>

      {officer && !officer.published && (
        <div className="rounded-lg border border-danger/50 bg-danger/10 p-4 text-sm">
          <p className="font-semibold text-ink">No Grievance Officer is published.</p>
          <p className="mt-1 text-muted">
            The Act requires a named officer with a monitored address. Until one is
            set, escalations are recorded but cannot be delivered to anybody — they
            appear in the notification log as suppressed.
          </p>
        </div>
      )}

      {error && (
        <div className="rounded-lg border border-danger/50 bg-danger/10 p-3 text-sm text-ink">
          {error}
        </div>
      )}

      {/* ------------------------------------------------------- headline -- */}
      <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-5">
        {[
          { label: "Open", value: counts.open, tone: "text-ink" },
          { label: "Overdue", value: counts.overdue, tone: "text-danger" },
          { label: "Escalated", value: counts.escalated, tone: "text-warning" },
          {
            label: "Awaiting confirmation",
            value: counts.awaiting_confirmation,
            tone: "text-muted",
            title:
              "Filed without an account. Recorded and counted, but will not escalate until the person confirms their email address.",
          },
          {
            label: "Confirmation expired",
            value: counts.confirmation_expired,
            tone: "text-muted",
            title:
              "The confirmation window has closed, so these will never be confirmed and never escalate. If they look genuine, they need picking up by hand.",
          },
        ].map((c) => (
          <div key={c.label} className="card p-4" title={c.title}>
            <p className="text-xs text-muted">{c.label}</p>
            <p className={`mt-1 text-2xl font-semibold ${c.tone}`}>{c.value ?? 0}</p>
          </div>
        ))}
      </div>

      {attention > 0 && (
        <p className="text-xs text-danger">
          {attention} complaint{attention === 1 ? "" : "s"} past a statutory
          threshold.
        </p>
      )}

      {/* -------------------------------------------------------- filters -- */}
      <div className="flex flex-wrap gap-2">
        {FILTERS.map((f) => (
          <button
            key={f.id}
            type="button"
            onClick={() => setFilter(f.id)}
            className={`rounded-full px-4 py-1.5 text-sm transition ${
              filter === f.id
                ? "bg-navy text-white"
                : "border border-line bg-surface text-ink hover:bg-line/40"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* ---------------------------------------------------------- table -- */}
      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-line">
            <thead className="bg-canvas">
              <tr>
                <th className="th">Reference</th>
                <th className="th">About</th>
                <th className="th">From</th>
                <th className="th">Status</th>
                <th className="th">Open</th>
                <th className="th">Due</th>
                <th className="th">Assigned</th>
                <th className="th sr-only">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {rows.length === 0 && (
                <tr>
                  <td className="td text-center text-muted" colSpan={8}>
                    Nothing here.
                  </td>
                </tr>
              )}
              {rows.map((g) => (
                <tr key={g.id} className={g.escalated ? "bg-danger/5" : ""}>
                  <td className="td font-mono text-xs">{g.reference}</td>
                  <td className="td">{CATEGORY_LABEL[g.category] || g.category}</td>
                  <td className="td text-xs">
                    {g.contact_email || "account holder"}
                    {!g.contact_verified && g.contact_email && (
                      <span
                        className="ml-1 text-warning"
                        title="This address has not been confirmed, so this complaint will not escalate."
                      >
                        unconfirmed
                      </span>
                    )}
                  </td>
                  <td className="td">
                    <div className="flex flex-wrap gap-1.5">
                      <StatusBadge
                        status={g.status}
                        label={STATUS_LABEL[g.status] || g.status}
                      />
                      {g.escalated && <StatusBadge status="escalated" />}
                    </div>
                  </td>
                  <td className="td text-xs">{g.days_open}d</td>
                  <td className={`td text-xs ${g.is_overdue ? "text-danger" : "text-muted"}`}>
                    {new Date(g.deadline_at).toLocaleDateString()}
                  </td>
                  <td className="td text-xs text-muted">
                    {g.assigned_to ? assignedName(g.assigned_to) : "—"}
                  </td>
                  <td className="td">
                    <button
                      type="button"
                      className="text-sm text-teal underline"
                      onClick={() => setSelected(g)}
                    >
                      Open
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* ----------------------------------------------------- detail pane -- */}
      <SlideOver
        open={Boolean(selected)}
        onClose={() => setSelected(null)}
        title={selected ? selected.reference : ""}
      >
        {selected && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge
                status={selected.status}
                label={STATUS_LABEL[selected.status] || selected.status}
              />
              {selected.escalated && <StatusBadge status="escalated" />}
              {selected.is_overdue && <StatusBadge status="overdue" />}
            </div>

            <dl className="grid grid-cols-2 gap-3 text-sm">
              <div>
                <dt className="text-xs text-muted">About</dt>
                <dd className="text-ink">
                  {CATEGORY_LABEL[selected.category] || selected.category}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted">Filed</dt>
                <dd className="text-ink">
                  {new Date(selected.submitted_at).toLocaleDateString()}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted">Response due</dt>
                <dd className={selected.is_overdue ? "text-danger" : "text-ink"}>
                  {new Date(selected.deadline_at).toLocaleDateString()}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted">Escalates</dt>
                <dd className="text-ink">
                  {new Date(selected.escalate_at).toLocaleDateString()}
                </dd>
              </div>
            </dl>

            {/* Raw from the API, escaped by React. Written by a member of the
                public, and may name third parties — see the model docstring. */}
            <div className="rounded-lg border border-line bg-canvas p-3">
              <p className="text-xs text-muted">In their words</p>
              <p className="mt-1 whitespace-pre-wrap text-sm text-ink">
                {selected.description}
              </p>
            </div>

            {selected.contact_email && !selected.contact_verified && (
              <p className="rounded-lg border border-warning/40 bg-warning/5 p-3 text-xs text-muted">
                Filed without an account and the address{" "}
                <span className="font-mono">{selected.contact_email}</span> has not
                been confirmed. It is recorded and the deadline is running, but it
                will not escalate automatically. Treat it on its merits.
              </p>
            )}

            {/* ------------------------------------------------ assignment -- */}
            {users.length > 0 && (
              <div>
                <label className="label" htmlFor="g-assign">
                  Assigned to
                </label>
                <select
                  id="g-assign"
                  className="input"
                  value={selected.assigned_to || ""}
                  onChange={(e) =>
                    act(
                      () => assignGrievance(selected.id, e.target.value || null),
                      e.target.value ? "Assigned." : "Unassigned.",
                    )
                  }
                  disabled={busy}
                >
                  <option value="">Unassigned</option>
                  {users
                    .filter((u) => u.is_active !== false)
                    .map((u) => (
                      <option key={u.id} value={u.id}>
                        {u.email}
                      </option>
                    ))}
                </select>
              </div>
            )}

            {/* -------------------------------------------------- timeline -- */}
            {selected.timeline?.length > 0 && (
              <div>
                <p className="text-sm font-semibold text-ink">Timeline</p>
                <ul className="mt-2 space-y-2">
                  {selected.timeline.map((e, i) => (
                    <li
                      key={i}
                      className="rounded-lg border border-line px-3 py-2 text-xs"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-ink">
                          {e.from_status && e.from_status !== e.to_status
                            ? `${STATUS_LABEL[e.from_status] || e.from_status} → ${
                                STATUS_LABEL[e.to_status] || e.to_status
                              }`
                            : STATUS_LABEL[e.to_status] || e.to_status}
                        </span>
                        {/* "The system did this" and "a human decided this" are
                            different facts. */}
                        {e.automated && <span className="tag">automatic</span>}
                        <span className="ml-auto text-muted">
                          {new Date(e.created_at).toLocaleString()}
                        </span>
                      </div>
                      {e.note && <p className="mt-1 text-muted">{e.note}</p>}
                      {e.actor_label && (
                        <p className="mt-0.5 text-muted">by {e.actor_label}</p>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {/* ---------------------------------------------------- action -- */}
            {NEXT[selected.status]?.length > 0 ? (
              <div className="space-y-3 border-t border-line pt-4">
                {NEXT[selected.status].includes("resolved") && (
                  <div>
                    <label className="label" htmlFor="g-notes">
                      How was it resolved?
                    </label>
                    <textarea
                      id="g-notes"
                      className="input min-h-[100px]"
                      value={notes}
                      maxLength={8000}
                      onChange={(e) => setNotes(e.target.value)}
                      placeholder="What you did, and what the person can expect now."
                    />
                    <p className="mt-1 text-xs text-muted">
                      Required. This is sent to the person and kept as the record
                      of the redress.
                    </p>
                  </div>
                )}

                {NEXT[selected.status].includes("rejected") && (
                  <div>
                    <label className="label" htmlFor="g-reason">
                      Reason for not upholding
                    </label>
                    <textarea
                      id="g-reason"
                      className="input min-h-[80px]"
                      value={reason}
                      maxLength={4000}
                      onChange={(e) => setReason(e.target.value)}
                      placeholder="Why this complaint cannot be upheld."
                    />
                    <p className="mt-1 text-xs text-muted">
                      Required. After this the person&rsquo;s next step is the Data
                      Protection Board, and they are entitled to know why.
                    </p>
                  </div>
                )}

                <div className="flex flex-wrap gap-2">
                  {NEXT[selected.status]
                    .filter((s) => s !== "rejected")
                    .map((s) => (
                      <button
                        key={s}
                        type="button"
                        className={s === "resolved" ? "btn-primary" : "btn-secondary"}
                        onClick={() => move(s)}
                        disabled={busy || (s === "resolved" && !notes.trim())}
                      >
                        {s === "resolved"
                          ? "Resolve"
                          : `Mark ${STATUS_LABEL[s] || s}`}
                      </button>
                    ))}
                  {NEXT[selected.status].includes("rejected") && (
                    <button
                      type="button"
                      className="btn-ghost text-danger"
                      onClick={() => setConfirmReject(true)}
                      disabled={busy || !reason.trim()}
                    >
                      Do not uphold
                    </button>
                  )}
                  {!selected.escalated && (
                    <button
                      type="button"
                      className="btn-ghost"
                      onClick={() => setConfirmEscalate(true)}
                      disabled={busy}
                    >
                      Escalate now
                    </button>
                  )}
                </div>
              </div>
            ) : (
              <p className="border-t border-line pt-4 text-xs text-muted">
                {selected.status === "rejected"
                  ? "This complaint was not upheld and is closed. It cannot be reopened here — the person's next step is the Data Protection Board."
                  : "Resolved. It can only be reopened by the person who filed it, by rating the outcome."}
              </p>
            )}
          </div>
        )}
      </SlideOver>

      <ConfirmModal
        open={confirmEscalate}
        title="Escalate to the Grievance Officer?"
        body={
          officer?.published
            ? `${officer.name} (${officer.email}) will be emailed. This does not contact the Data Protection Board — that stays a human decision.`
            : "No Grievance Officer is published, so this will be recorded but cannot be delivered to anybody."
        }
        confirmLabel="Escalate"
        onCancel={() => setConfirmEscalate(false)}
        onConfirm={() =>
          act(
            () => escalateGrievance(selected.id, "Escalated manually from the queue."),
            "Escalated.",
          )
        }
      />

      <ConfirmModal
        open={confirmReject}
        title="Record this complaint as not upheld?"
        body="The person will be emailed the reason, and told they may approach the Data Protection Board."
        consequences={[
          "This cannot be undone — a complaint that was not upheld is closed.",
          "They are told their next step is the Data Protection Board.",
        ]}
        confirmLabel="Do not uphold"
        onCancel={() => setConfirmReject(false)}
        onConfirm={() => move("rejected")}
      />
    </div>
  );
}
