// ============================================================================
// StatCard — one number, with an optional tone and link. Used by both
// dashboards' stat strips.
// ============================================================================
import { Link } from "react-router-dom";

const TONE = {
  neutral: { dot: "bg-navy", value: "text-ink" },
  success: { dot: "bg-success", value: "text-ink" },
  warning: { dot: "bg-warning", value: "text-ink" },
  danger: { dot: "bg-danger", value: "text-danger" },
  info: { dot: "bg-info", value: "text-ink" },
};

export default function StatCard({ label, value, tone = "neutral", to, hint, badge }) {
  const t = TONE[tone] || TONE.neutral;
  const body = (
    <div className="card h-full p-5 transition hover:border-navy/30">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className={`h-2 w-2 rounded-full ${t.dot}`} aria-hidden="true" />
          <p className="text-sm text-muted">{label}</p>
        </div>
        {badge}
      </div>
      <p className={`mt-2 text-3xl font-semibold ${t.value}`}>{value}</p>
      {hint && <p className="mt-1 text-xs text-muted">{hint}</p>}
    </div>
  );
  return to ? <Link to={to} className="block h-full">{body}</Link> : body;
}
