// ============================================================================
// SLACountdown — the legal clock on a DSAR (submitted_at + 30 days).
// green > 5 days · amber 2–5 days · red < 2 days · OVERDUE past the deadline.
// Colour is paired with the text, so it never carries the meaning alone.
// ============================================================================
const HOUR = 36e5;
const DAY = 864e5;

export function slaTone(deadlineAt) {
  const left = new Date(deadlineAt) - Date.now();
  if (left <= 0) return "overdue";
  if (left < 2 * DAY) return "red";
  if (left < 5 * DAY) return "amber";
  return "green";
}

const TONE = {
  green: { dot: "bg-success", text: "text-ink" },
  amber: { dot: "bg-warning", text: "text-ink" },
  red: { dot: "bg-danger", text: "text-danger font-semibold" },
  overdue: { dot: "bg-danger animate-pulse", text: "text-danger font-semibold" },
};

export default function SLACountdown({ deadlineAt, showDate = false }) {
  const tone = slaTone(deadlineAt);
  const left = new Date(deadlineAt) - Date.now();
  const days = Math.floor(Math.abs(left) / DAY);
  const hours = Math.floor((Math.abs(left) % DAY) / HOUR);

  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-sm">
      <span className={`h-2 w-2 shrink-0 rounded-full ${TONE[tone].dot}`} aria-hidden="true" />
      <span className={TONE[tone].text}>
        {tone === "overdue"
          ? `OVERDUE by ${days}d ${hours}h`
          : `${days} days ${hours} hours remaining`}
      </span>
      {showDate && (
        <span className="text-muted">
          (by {new Date(deadlineAt).toLocaleDateString()})
        </span>
      )}
    </span>
  );
}
