// ============================================================================
// TimelineTracker — the step tracker for a DSAR or a grievance.
// Steps carry a label AND a state (done / current / upcoming / failed), so the
// progress is readable without relying on colour.
// ============================================================================
export const DSAR_STEPS = ["Submitted", "Verified", "In Progress", "Completed"];
export const GRIEVANCE_STEPS = ["Submitted", "Acknowledged", "In Progress", "Resolved"];

// Maps a status to how far along the tracker it sits.
export function stepIndexFor(status, steps = DSAR_STEPS) {
  const map = {
    pending: 1,          // submitted, awaiting review
    submitted: 0,
    verified: 1,
    in_progress: 2,
    completed: 3,
    resolved: 3,
    open: 1,
    acknowledged: 1,
    rejected: 3,
    escalated: 3,
  };
  const i = map[String(status).toLowerCase()];
  return typeof i === "number" ? Math.min(i, steps.length - 1) : 0;
}

export default function TimelineTracker({
  steps = DSAR_STEPS,
  status,
  failed = false,      // rejected / escalated — the last step is a bad outcome
  failedLabel = "Rejected",
}) {
  const active = stepIndexFor(status, steps);
  const shown = failed ? [...steps.slice(0, -1), failedLabel] : steps;

  return (
    <ol className="flex flex-col gap-0 sm:flex-row sm:items-start">
      {shown.map((label, i) => {
        const done = i < active;
        const current = i === active;
        const isBad = failed && i === shown.length - 1 && current;

        const dot = isBad
          ? "bg-danger text-white"
          : done
            ? "bg-success text-white"
            : current
              ? "bg-info text-white"
              : "bg-line text-muted";

        return (
          <li key={label} className="flex flex-1 gap-3 sm:flex-col sm:gap-2">
            <div className="flex items-center gap-0 sm:w-full">
              <span
                className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${dot}`}
              >
                {isBad ? "✕" : done ? "✓" : i + 1}
              </span>
              {i < shown.length - 1 && (
                <span
                  className={`hidden h-0.5 flex-1 sm:block ${done ? "bg-success" : "bg-line"}`}
                  aria-hidden="true"
                />
              )}
            </div>
            <div className="pb-4 sm:pb-0">
              <p
                className={`text-sm ${current ? "font-semibold text-ink" : done ? "text-ink" : "text-muted"}`}
              >
                {label}
              </p>
              <p className="text-xs text-muted">
                {isBad ? "Outcome" : done ? "Done" : current ? "Current step" : "Upcoming"}
              </p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
