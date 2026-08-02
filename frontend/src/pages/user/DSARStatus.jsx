// ============================================================================
// DSAR Request Status (/user/dsar/status)
// List of the user's requests; click one for the tracker, SLA countdown,
// rejection reason, download link, and its notification history.
// ============================================================================
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getDSARRequests, getNotifications, MOCK_USER } from "../../api";
import StatusBadge from "../../components/common/StatusBadge";
import SLACountdown from "../../components/common/SLACountdown";
import TimelineTracker, { DSAR_STEPS } from "../../components/common/TimelineTracker";

const TYPE_LABEL = { access: "Access", correct: "Correction", erase: "Erasure" };

export default function DSARStatus() {
  const [rows, setRows] = useState([]);
  const [selected, setSelected] = useState(null);
  const [notifications, setNotifications] = useState([]);

  useEffect(() => {
    getDSARRequests({ userId: MOCK_USER.id }).then((r) => {
      setRows(r);
      setSelected((prev) => prev || r[0] || null);
    });
    getNotifications("user").then(setNotifications);
  }, []);

  const related = selected
    ? notifications.filter((n) => n.subject.includes(selected.reference))
    : [];

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-ink">My data requests</h1>
        <p className="text-sm text-muted">
          Track progress and the legal deadline for each request.
        </p>
      </div>

      {rows.length === 0 && (
        <div className="card p-6 text-center">
          <p className="text-sm text-muted">You haven&apos;t submitted any requests yet.</p>
          <Link to="/user/dsar" className="btn-primary mt-4">Submit a data request</Link>
        </div>
      )}

      {rows.length > 0 && (
        <div className="grid gap-5 lg:grid-cols-5">
          {/* list */}
          <div className="space-y-3 lg:col-span-2">
            {rows.map((r) => (
              <button
                key={r.id}
                type="button"
                onClick={() => setSelected(r)}
                className={`card w-full p-4 text-left transition ${
                  selected?.id === r.id ? "border-navy/50 ring-1 ring-navy/20" : "hover:border-navy/30"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono text-xs text-muted">{r.reference}</span>
                  <StatusBadge status={r.status} />
                </div>
                <p className="mt-2 font-medium text-ink">{TYPE_LABEL[r.type] || r.type} request</p>
                <p className="mt-1 text-xs text-muted">
                  Submitted {new Date(r.submitted_at).toLocaleDateString()}
                </p>
                <div className="mt-2">
                  {r.status === "completed" || r.status === "rejected" ? (
                    <span className="text-xs text-muted">
                      Closed {r.resolved_at ? new Date(r.resolved_at).toLocaleDateString() : "—"}
                    </span>
                  ) : (
                    <SLACountdown deadlineAt={r.deadline_at} />
                  )}
                </div>
              </button>
            ))}
          </div>

          {/* detail */}
          {selected && (
            <div className="space-y-4 lg:col-span-3">
              <div className="card p-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h2 className="font-semibold text-ink">
                      {TYPE_LABEL[selected.type] || selected.type} request
                    </h2>
                    <p className="font-mono text-xs text-muted">{selected.reference}</p>
                  </div>
                  <StatusBadge status={selected.status} />
                </div>

                <div className="mt-5">
                  <TimelineTracker
                    steps={DSAR_STEPS}
                    status={selected.status}
                    failed={selected.status === "rejected"}
                    failedLabel="Rejected"
                  />
                </div>

                <dl className="mt-4 grid gap-3 border-t border-line pt-4 text-sm sm:grid-cols-2">
                  <div>
                    <dt className="text-xs text-muted">Submitted</dt>
                    <dd>{new Date(selected.submitted_at).toLocaleString()}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-muted">Legal deadline</dt>
                    <dd>
                      {new Date(selected.deadline_at).toLocaleDateString()}
                      <div className="mt-1">
                        {selected.status === "completed" || selected.status === "rejected" ? (
                          <span className="text-xs text-muted">Closed</span>
                        ) : (
                          <SLACountdown deadlineAt={selected.deadline_at} />
                        )}
                      </div>
                    </dd>
                  </div>
                  <div>
                    <dt className="text-xs text-muted">Identity verified by</dt>
                    <dd className="capitalize">{selected.verification || "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-muted">Handled by</dt>
                    <dd>Data Protection Officer</dd>
                  </div>
                </dl>

                {selected.status === "rejected" && (
                  <div className="mt-4 rounded-lg border border-danger/40 bg-danger/5 p-3 text-sm">
                    <p className="font-medium text-ink">Why this was rejected</p>
                    <p className="mt-1 text-muted">
                      {selected.rejection_reason ||
                        "The request could not be completed. Contact the Grievance Officer if you disagree."}
                    </p>
                  </div>
                )}

                {selected.status === "completed" && selected.type === "access" && (
                  <div className="mt-4 rounded-lg border border-success/40 bg-success/5 p-3">
                    <p className="text-sm font-medium text-ink">Your data is ready</p>
                    <a href={selected.export_url || "#"} className="btn-secondary mt-2">
                      Download my data export
                    </a>
                  </div>
                )}

                {selected.status === "completed" && selected.type === "correct" && (
                  <div className="mt-4 rounded-lg border border-success/40 bg-success/5 p-3 text-sm">
                    <p className="font-medium text-ink">Correction applied</p>
                    <p className="mt-1 text-muted">
                      {selected.correction
                        ? `${selected.correction.field}: “${selected.correction.current}” → “${selected.correction.corrected}”`
                        : "The requested correction has been made."}
                    </p>
                  </div>
                )}

                {selected.status === "completed" && selected.type === "erase" && (
                  <div className="mt-4 rounded-lg border border-success/40 bg-success/5 p-3 text-sm">
                    <p className="font-medium text-ink">Erasure completed</p>
                    <p className="mt-1 text-muted">
                      Your personal data has been erased, except records we are legally required to
                      keep. Those are listed in the confirmation email.
                    </p>
                  </div>
                )}

                {/* When wired to the real Fides backend, the per-collection log
                    from this repo's gateway shows up here as the proof. */}
                {selected.execution_log?.length > 0 && (
                  <div className="mt-4 border-t border-line pt-4">
                    <p className="text-sm font-medium text-ink">Execution log</p>
                    <ul className="mt-2 space-y-1 text-xs text-muted">
                      {selected.execution_log
                        .filter((e) => e.collection)
                        .map((e, i) => (
                          <li key={i}>
                            {e.dataset}:{e.collection} — {e.action_type} — {e.status}
                          </li>
                        ))}
                    </ul>
                  </div>
                )}
              </div>

              <div className="card p-5">
                <p className="text-sm font-semibold text-ink">Notification history</p>
                {related.length === 0 ? (
                  <p className="mt-2 text-sm text-muted">
                    No notifications recorded for this request yet.
                  </p>
                ) : (
                  <ul className="mt-3 divide-y divide-line">
                    {related.map((n) => (
                      <li key={n.id} className="flex flex-wrap items-center gap-2 py-2 text-sm">
                        <StatusBadge status={n.status} />
                        <span className="text-ink">{n.subject}</span>
                        <span className="text-xs text-muted">
                          {n.channel} · {new Date(n.sent_at).toLocaleString()}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
