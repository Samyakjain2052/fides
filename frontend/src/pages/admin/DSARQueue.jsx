// ============================================================================
// DSAR triage queue (/admin/dsar)
//
// Rewritten against the real API. It was previously reading the mock
// `getDSARRequests()` while the module was flagged live — so a request filed
// through the real user portal landed in PostgreSQL and never appeared here. File
// as a user, look as an admin, and it was not there.
//
// It was also half-migrated and broken: `changeStatus` and `downloadPackage` were
// called but never imported, so "Save & Notify User" threw a ReferenceError.
//
// Four things this version stops inventing:
//
//   * **The status options come from the server.** `allowed_transitions` per
//     request, so the control cannot offer a move the state machine will refuse.
//     The old list included "pending", which is not a status this product has.
//   * **Identity verification is reported, not asserted.** The old badge rendered
//     "OTP verified" from the method alone — the method is what was asked for,
//     `verified_at` is what actually happened. Unverified now says so.
//   * **The per-request history is the server's timeline**, not a filtered slice
//     of a mock audit log.
//   * **The "exempt under law" toggle is gone.** It stored nothing; it appended a
//     sentence to a note while implying a recorded legal exemption. A note field
//     that says it is a note replaces it.
// ============================================================================
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  changeStatus,
  downloadPackage,
  getRequest,
  queueRows,
  retryDispatch,
} from "../../api/dsar";
import { useApp } from "../../context/AppContext";
import StatusBadge from "../../components/common/StatusBadge";
import SLACountdown, { slaTone } from "../../components/common/SLACountdown";
import SlideOver from "../../components/common/SlideOver";
import ConfirmModal from "../../components/common/ConfirmModal";

// Server vocabulary. `erasure` and `correction`, not `erase` and `correct` — the
// old screen filtered on names the API never returns, so those filters matched
// nothing.
const TYPE_FILTERS = [
  { id: "", label: "All" },
  { id: "access", label: "Access" },
  { id: "correction", label: "Correction" },
  { id: "erasure", label: "Erasure" },
];

const STATUS_FILTERS = [
  { id: "", label: "Any status" },
  { id: "received", label: "Received" },
  { id: "verifying", label: "Verifying" },
  { id: "in_progress", label: "In progress" },
  { id: "completed", label: "Completed" },
  { id: "rejected", label: "Rejected" },
  { id: "cancelled", label: "Cancelled" },
];

const STATUS_LABEL = Object.fromEntries(
  STATUS_FILTERS.filter((s) => s.id).map((s) => [s.id, s.label]),
);

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
  const [total, setTotal] = useState(0);
  const [type, setType] = useState("");
  const [status, setStatus] = useState("");
  const [overdueOnly, setOverdueOnly] = useState(false);
  const [q, setQ] = useState("");
  const [sort, setSort] = useState("deadline_at");
  const [dir, setDir] = useState("asc");
  const [selected, setSelected] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // panel state
  const [nextStatus, setNextStatus] = useState("");
  const [rejection, setRejection] = useState("");
  const [note, setNote] = useState("");
  const [confirmErase, setConfirmErase] = useState(false);

  const load = useCallback(async () => {
    try {
      const page = await queueRows({ status, type, overdueOnly });
      setRows(page.rows);
      setTotal(page.total);
    } catch (e) {
      setError(e.message);
    }
  }, [status, type, overdueOnly]);

  useEffect(() => {
    load();
  }, [load]);

  // The panel needs the detail shape — the list rows carry no timeline and no
  // allowed transitions, and guessing either would put this screen back in the
  // business of re-implementing the state machine.
  const open = async (row) => {
    setError("");
    try {
      const detail = await getRequest(row.id);
      setSelected(detail);
    } catch (e) {
      setError(e.message);
    }
  };

  useEffect(() => {
    if (!selected) return;
    setNextStatus("");
    setRejection(selected.rejection_reason || "");
    setNote("");
  }, [selected]);

  // Search is client-side over the fetched page; the filters are server-side.
  // Reference and email are the two things somebody reads off a phone call.
  const shown = useMemo(() => {
    let out = [...rows];
    if (q) {
      const needle = q.toLowerCase();
      out = out.filter(
        (r) =>
          r.reference.toLowerCase().includes(needle) ||
          (r.user_email || "").toLowerCase().includes(needle),
      );
    }
    out.sort((a, b) => {
      const av = a[sort] ?? "";
      const bv = b[sort] ?? "";
      return dir === "asc"
        ? String(av).localeCompare(String(bv))
        : String(bv).localeCompare(String(av));
    });
    return out;
  }, [rows, q, sort, dir]);

  const toggleSort = (id) => {
    if (sort === id) setDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSort(id);
      setDir("asc");
    }
  };

  const act = async (fn, message) => {
    setBusy(true);
    setError("");
    try {
      const updated = await fn();
      if (updated?.id) setSelected(updated);
      await load();
      if (message) notify(message);
      return updated;
    } catch (e) {
      // The server gives real reasons: an illegal transition names what IS
      // allowed, a reasonless rejection says so. Passing them through is the
      // difference between a usable queue and a mystery.
      setError(e.message);
      return null;
    } finally {
      setBusy(false);
    }
  };

  const move = (target) =>
    act(
      () =>
        changeStatus(selected.id, {
          toStatus: target,
          // Required by both the server and a CHECK constraint. Sending it is
          // not optional.
          reason: target === "rejected" ? rejection : undefined,
          note: note.trim() || undefined,
        }),
      target === "rejected"
        ? "Rejected. The person has been emailed the reason."
        : target === "completed"
          ? "Completed. The person has been emailed."
          : `Moved to ${STATUS_LABEL[target] || target}.`,
    );

  const allowed = selected?.allowed_transitions ?? [];

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-ink">Data request queue</h1>
        <p className="text-sm text-muted">
          Every rights request with its statutory deadline, soonest first. The
          deadline comes from this workspace&rsquo;s configured response window,
          not a fixed 30 days.
        </p>
      </div>

      {error && (
        <div className="rounded-lg border border-danger/50 bg-danger/10 p-3 text-sm text-ink">
          {error}
        </div>
      )}

      <div className="card space-y-3 p-4">
        <div className="flex flex-wrap gap-2">
          {TYPE_FILTERS.map((f) => (
            <button
              key={f.id || "all"}
              type="button"
              onClick={() => setType(f.id)}
              className={`rounded-full px-3 py-1.5 text-sm transition ${
                type === f.id
                  ? "bg-navy text-white"
                  : "border border-line bg-surface text-ink hover:bg-line/40"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label className="label" htmlFor="dsar-status">Status</label>
            <select
              id="dsar-status"
              className="input"
              value={status}
              onChange={(e) => setStatus(e.target.value)}
            >
              {STATUS_FILTERS.map((s) => (
                <option key={s.id || "any"} value={s.id}>{s.label}</option>
              ))}
            </select>
          </div>
          <label className="flex items-center gap-2 pb-2 text-sm text-ink">
            <input
              type="checkbox"
              checked={overdueOnly}
              onChange={(e) => setOverdueOnly(e.target.checked)}
            />
            Past the deadline only
          </label>
          <div className="min-w-[16rem] flex-1">
            <label className="sr-only" htmlFor="dsar-search">Search</label>
            <input
              id="dsar-search"
              className="input"
              placeholder="Search this page by reference or email…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          </div>
        </div>
        <p className="text-xs text-muted">
          {shown.length === total
            ? `${total} request${total === 1 ? "" : "s"}`
            : `${shown.length} of ${total} shown`}
        </p>
      </div>

      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-line">
            <thead className="bg-canvas">
              <tr>
                {SORTS.map((s) => (
                  <th key={s.id} className="th">
                    <button
                      type="button"
                      className="inline-flex items-center gap-1"
                      onClick={() => toggleSort(s.id)}
                    >
                      {s.label}
                      {sort === s.id && (
                        <span aria-hidden="true">{dir === "asc" ? "↑" : "↓"}</span>
                      )}
                    </button>
                  </th>
                ))}
                <th className="th">Person</th>
                <th className="th">Deadline</th>
                <th className="th sr-only">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {shown.length === 0 && (
                <tr>
                  <td className="td text-center text-muted" colSpan={8}>
                    No requests match this view.
                  </td>
                </tr>
              )}
              {shown.map((r) => (
                <tr
                  key={r.id}
                  onClick={() => open(r)}
                  className={`cursor-pointer hover:bg-canvas ${
                    r.overdue ? "bg-danger/5" : ""
                  }`}
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
                    {/* `overdue` is computed server-side against the clock on
                        every read, so it cannot lag behind a job. */}
                    <StatusBadge
                      status={r.overdue ? "overdue" : r.status}
                      label={r.overdue ? "Overdue" : STATUS_LABEL[r.status] || r.status}
                    />
                  </td>
                  <td className="td text-xs text-muted">
                    {r.user_email || r.user_id || "—"}
                  </td>
                  <td className="td">
                    {["completed", "rejected", "cancelled"].includes(r.status) ? (
                      <span className="text-xs text-muted">closed</span>
                    ) : (
                      <SLACountdown deadlineAt={r.deadline_at} />
                    )}
                  </td>
                  <td className="td">
                    <span className="text-sm text-teal underline">View</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* --------------------------------------------------- detail panel -- */}
      <SlideOver
        open={Boolean(selected)}
        title={selected ? `${selected.type} — ${selected.reference}` : ""}
        subtitle={selected?.principal_email}
        onClose={() => setSelected(null)}
      >
        {selected && (
          <div className="space-y-5">
            <dl className="grid gap-3 text-sm sm:grid-cols-2">
              <div>
                <dt className="text-xs text-muted">Type</dt>
                <dd className="capitalize text-ink">{selected.type}</dd>
              </div>
              <div>
                <dt className="text-xs text-muted">Raised by</dt>
                <dd className="text-ink">
                  {selected.requested_by_actor === "staff"
                    ? "staff, on their behalf"
                    : "the person themselves"}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted">Submitted</dt>
                <dd className="text-ink">
                  {new Date(selected.submitted_at).toLocaleString()}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted">Deadline</dt>
                <dd className="text-ink">
                  {new Date(selected.deadline_at).toLocaleDateString()}
                  <div className="mt-1">
                    {["completed", "rejected", "cancelled"].includes(selected.status) ? (
                      // A closed request has no live clock. Showing "OVERDUE"
                      // against something already answered would misreport
                      // compliance in the direction that looks worse than reality.
                      <span className="text-xs text-muted">
                        Closed
                        {selected.resolved_at
                          ? ` ${new Date(selected.resolved_at).toLocaleDateString()}`
                          : ""}
                        {selected.resolved_at &&
                        new Date(selected.resolved_at) <= new Date(selected.deadline_at)
                          ? " — within the deadline"
                          : ""}
                      </span>
                    ) : (
                      <SLACountdown deadlineAt={selected.deadline_at} />
                    )}
                  </div>
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted">Identity check</dt>
                <dd>
                  {/* Reported, not asserted. `verification_method` is what was
                      asked for; `verified_at` is what happened. */}
                  {selected.verified_at ? (
                    <StatusBadge
                      status="verified"
                      label={`${selected.verification_method || "verified"} · ${new Date(
                        selected.verified_at,
                      ).toLocaleDateString()}`}
                    />
                  ) : (
                    <StatusBadge status="pending" label="Not verified" />
                  )}
                </dd>
              </div>
              {selected.engine_ref && (
                <div>
                  <dt className="text-xs text-muted">Engine reference</dt>
                  <dd className="font-mono text-xs text-ink">{selected.engine_ref}</dd>
                </div>
              )}
            </dl>

            {/* The engine failed — the request survived, and can be re-dispatched. */}
            {selected.engine_error && (
              <div className="rounded-lg border border-danger/40 bg-danger/5 p-3">
                <p className="text-sm font-semibold text-ink">
                  The privacy engine could not be reached
                </p>
                <p className="mt-1 text-xs text-danger">{selected.engine_error}</p>
                <p className="mt-1 text-xs text-muted">
                  The request was not lost — it is still recorded and its deadline
                  is still running.
                </p>
                <button
                  type="button"
                  className="btn-secondary mt-2"
                  disabled={busy}
                  onClick={() =>
                    act(() => retryDispatch(selected.id), "Re-dispatched to the engine.")
                  }
                >
                  Try again
                </button>
              </div>
            )}

            {/* ----------------------------------------- type-specific work -- */}
            <div className="rounded-lg border border-line p-4">
              <p className="text-sm font-semibold text-ink">What this request needs</p>

              {selected.type === "access" && (
                <div className="mt-2 space-y-2">
                  <p className="text-sm text-muted">
                    The engine collects this person&rsquo;s data across every
                    connected datastore. The package expires, and every retrieval
                    is audited.
                  </p>
                  {selected.package_available_until ? (
                    <>
                      <button
                        type="button"
                        className="btn-secondary"
                        disabled={busy}
                        onClick={() =>
                          act(
                            () => downloadPackage(selected.id, selected.reference),
                            "Package downloaded. The retrieval is in the audit trail.",
                          )
                        }
                      >
                        Download the access package
                      </button>
                      <p className="text-xs text-muted">
                        Available until{" "}
                        {new Date(selected.package_available_until).toLocaleString()}.
                      </p>
                    </>
                  ) : (
                    <p className="text-xs text-muted">
                      No package yet — one is produced when the request completes.
                    </p>
                  )}
                </div>
              )}

              {selected.type === "correction" && (
                <div className="mt-2 space-y-2">
                  {selected.correction_payload ? (
                    <div className="rounded-lg bg-canvas p-3 text-sm">
                      <p className="text-xs text-muted">Requested correction</p>
                      <pre className="mt-1 whitespace-pre-wrap text-xs text-ink">
                        {JSON.stringify(selected.correction_payload, null, 2)}
                      </pre>
                    </div>
                  ) : (
                    <p className="text-sm text-muted">No correction details recorded.</p>
                  )}
                  <p className="text-xs text-warning">
                    Correction is a manual workflow — the privacy engine has no
                    correction action, so somebody has to make the change and then
                    complete the request.
                  </p>
                </div>
              )}

              {selected.type === "erasure" && (
                <div className="mt-2 space-y-2">
                  <p className="text-sm text-muted">
                    Erasure masks this person&rsquo;s identifiers across every
                    connected datastore. Records held under a legal obligation are
                    retained and reported back.
                  </p>
                  {selected.status === "received" && (
                    <button
                      type="button"
                      className="btn-danger"
                      onClick={() => setConfirmErase(true)}
                      disabled={busy}
                    >
                      Begin the erasure
                    </button>
                  )}
                </div>
              )}
            </div>

            {/* ----------------------------------------------------- action -- */}
            {allowed.length > 0 ? (
              <div className="space-y-3">
                <div>
                  <label className="label" htmlFor="p-note">
                    Note for the timeline (optional)
                  </label>
                  <input
                    id="p-note"
                    className="input"
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    placeholder="What you did, or why."
                  />
                </div>

                {allowed.includes("rejected") && (
                  <div>
                    <label className="label" htmlFor="p-reason">
                      Reason for rejection
                    </label>
                    <textarea
                      id="p-reason"
                      className="input min-h-[80px]"
                      value={rejection}
                      onChange={(e) => setRejection(e.target.value)}
                    />
                    <p className="mt-1 text-xs text-muted">
                      Required to reject, and emailed to the person. A rejection
                      with no recorded reason is not a decision anybody can defend.
                    </p>
                  </div>
                )}

                {/* Only what the server will accept. The screen no longer keeps
                    its own idea of the state machine. */}
                <div className="flex flex-wrap gap-2">
                  {allowed
                    .filter((s) => s !== "rejected")
                    .map((s) => (
                      <button
                        key={s}
                        type="button"
                        className={s === "completed" ? "btn-primary" : "btn-secondary"}
                        disabled={busy}
                        onClick={() => move(s)}
                      >
                        {s === "completed"
                          ? "Complete"
                          : `Move to ${STATUS_LABEL[s] || s}`}
                      </button>
                    ))}
                  {allowed.includes("rejected") && (
                    <button
                      type="button"
                      className="btn-ghost text-danger"
                      disabled={busy || !rejection.trim()}
                      onClick={() => move("rejected")}
                    >
                      Reject
                    </button>
                  )}
                </div>
              </div>
            ) : (
              <p className="text-xs text-muted">
                This request is closed. Nothing further can be changed — the record
                stands as it is.
              </p>
            )}

            {/* --------------------------------------------------- timeline -- */}
            <div>
              <p className="text-sm font-semibold text-ink">Timeline</p>
              {(selected.timeline || []).length === 0 ? (
                <p className="mt-1 text-sm text-muted">No entries yet.</p>
              ) : (
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
                        {/* "The engine moved this" and "a human decided this" are
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
              )}
            </div>
          </div>
        )}
      </SlideOver>

      <ConfirmModal
        open={confirmErase}
        title="Begin the erasure?"
        body={`This starts erasure for ${
          selected?.principal_email || "this person"
        } across every connected datastore.`}
        consequences={[
          "Identifiers are masked in each system that holds them.",
          "Records held under a legal obligation are retained, and reported back.",
          "The erasure is irreversible.",
          "Every datastore touched is written to the audit trail.",
        ]}
        confirmLabel="Yes, begin the erasure"
        busy={busy}
        onCancel={() => setConfirmErase(false)}
        onConfirm={async () => {
          await move("in_progress");
          setConfirmErase(false);
        }}
      />
    </div>
  );
}
