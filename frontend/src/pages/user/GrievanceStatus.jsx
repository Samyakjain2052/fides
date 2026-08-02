// ============================================================================
// Grievance Status (/user/grievance/status)
// Tracker, resolution notes, escalation notice, and a feedback rating once the
// complaint is resolved.
// ============================================================================
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  getGrievances,
  GRIEVANCE_ESCALATION_DAYS,
  MOCK_ORG,
  subjectIdentity,
  submitGrievanceFeedback,
} from "../../api";
import { useApp } from "../../context/AppContext";
import StatusBadge from "../../components/common/StatusBadge";
import TimelineTracker, { GRIEVANCE_STEPS } from "../../components/common/TimelineTracker";
import { previewLock } from "../../config/modules";

const DAY = 864e5;

export default function GrievanceStatus() {
  const { notify } = useApp();
  const [rows, setRows] = useState([]);
  const [selected, setSelected] = useState(null);
  const [rating, setRating] = useState(0);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);

  const load = () =>
    getGrievances({ userId: subjectIdentity().id }).then((r) => {
      setRows(r);
      setSelected((prev) => (prev ? r.find((x) => x.id === prev.id) || r[0] : r[0]) || null);
    });

  useEffect(() => {
    load();
  }, []);

  const sendFeedback = async () => {
    setBusy(true);
    try {
      await submitGrievanceFeedback(selected.id, { rating, comment });
      await load();
      notify("Thanks — your feedback has been recorded.");
      setRating(0);
      setComment("");
    } finally {
      setBusy(false);
    }
  };

  const daysOpen = selected ? Math.floor((Date.now() - new Date(selected.submitted_at)) / DAY) : 0;
  const overdue = selected && selected.status !== "resolved" && daysOpen > GRIEVANCE_ESCALATION_DAYS;

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-ink">My complaints</h1>
        <p className="text-sm text-muted">
          Grievance Officer: {MOCK_ORG.grievanceOfficer} · {MOCK_ORG.grievanceEmail}
        </p>
      </div>

      {rows.length === 0 && (
        <div className="card p-6 text-center">
          <p className="text-sm text-muted">You haven&apos;t filed any complaints.</p>
          <Link to="/user/grievance" className="btn-primary mt-4">File a complaint</Link>
        </div>
      )}

      {rows.length > 0 && (
        <div className="grid gap-5 lg:grid-cols-5">
          <div className="space-y-3 lg:col-span-2">
            {rows.map((g) => (
              <button
                key={g.id}
                type="button"
                onClick={() => setSelected(g)}
                className={`card w-full p-4 text-left transition ${
                  selected?.id === g.id ? "border-navy/50 ring-1 ring-navy/20" : "hover:border-navy/30"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono text-xs text-muted">{g.reference}</span>
                  <StatusBadge status={g.escalated ? "escalated" : g.status} />
                </div>
                <p className="mt-2 font-medium text-ink">{g.category}</p>
                <p className="mt-1 text-xs text-muted">
                  Filed {new Date(g.submitted_at).toLocaleDateString()} ·{" "}
                  {Math.floor((Date.now() - new Date(g.submitted_at)) / DAY)} days open
                </p>
              </button>
            ))}
          </div>

          {selected && (
            <div className="space-y-4 lg:col-span-3">
              <div className="card p-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h2 className="font-semibold text-ink">{selected.category}</h2>
                    <p className="font-mono text-xs text-muted">{selected.reference}</p>
                  </div>
                  <StatusBadge status={selected.escalated ? "escalated" : selected.status} />
                </div>

                <div className="mt-5">
                  <TimelineTracker
                    steps={GRIEVANCE_STEPS}
                    status={selected.status}
                    failed={selected.escalated}
                    failedLabel="Escalated"
                  />
                </div>

                <div className="mt-4 border-t border-line pt-4">
                  <p className="text-xs text-muted">Your complaint</p>
                  <p className="mt-1 text-sm text-ink">{selected.description}</p>
                  {selected.related_dsar && (
                    <p className="mt-2 text-xs text-muted">
                      Linked data request: <span className="font-mono">{selected.related_dsar}</span>
                    </p>
                  )}
                </div>

                {overdue && (
                  <div className="mt-4 rounded-lg border border-danger/40 bg-danger/5 p-3 text-sm">
                    <p className="flex items-center gap-2 font-medium text-ink">
                      <span className="h-2 w-2 rounded-full bg-danger" aria-hidden="true" />
                      Past our response window
                    </p>
                    <p className="mt-1 text-muted">
                      This has been open {daysOpen} days, beyond the {GRIEVANCE_ESCALATION_DAYS}-day
                      threshold, so it is being escalated to the Data Protection Officer. You may
                      also complain to the Data Protection Board of India.
                    </p>
                  </div>
                )}

                {selected.resolution_notes && (
                  <div className="mt-4 rounded-lg border border-success/40 bg-success/5 p-3 text-sm">
                    <p className="font-medium text-ink">Resolution</p>
                    <p className="mt-1 text-muted">{selected.resolution_notes}</p>
                  </div>
                )}
              </div>

              {selected.status === "resolved" && (
                <div className="card p-5">
                  <p className="text-sm font-semibold text-ink">
                    {selected.feedback ? "Your feedback" : "How did we do?"}
                  </p>

                  {selected.feedback ? (
                    <div className="mt-2 text-sm">
                      <p className="text-warning" aria-label={`${selected.feedback.rating} out of 5`}>
                        {"★".repeat(selected.feedback.rating)}
                        <span className="text-line">{"★".repeat(5 - selected.feedback.rating)}</span>
                      </p>
                      {selected.feedback.comment && (
                        <p className="mt-1 text-muted">{selected.feedback.comment}</p>
                      )}
                    </div>
                  ) : (
                    <>
                      <div className="mt-2 flex gap-1">
                        {[1, 2, 3, 4, 5].map((n) => (
                          <button
                            key={n}
                            type="button"
                            aria-label={`${n} star${n > 1 ? "s" : ""}`}
                            onClick={() => setRating(n)}
                            className={`text-2xl leading-none ${n <= rating ? "text-warning" : "text-line"}`}
                          >
                            ★
                          </button>
                        ))}
                      </div>
                      <textarea
                        className="input mt-3 min-h-[80px]"
                        value={comment}
                        onChange={(e) => setComment(e.target.value)}
                        placeholder="Anything you'd like to add (optional)"
                        aria-label="Feedback comment"
                      />
                      <button type="button" className="btn-primary mt-3" disabled={!rating || busy}
                              {...previewLock("grievance", "Sending feedback")}
                              onClick={sendFeedback}>
                        {busy ? "Sending…" : "Submit feedback"}
                      </button>
                    </>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
