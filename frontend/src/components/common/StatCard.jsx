// ============================================================================
// StatCard — one number, with an optional tone and link. Used by both
// dashboards' stat strips.
// ============================================================================
import { Link } from "react-router-dom";
import { SampleTag } from "./PreviewBanner";

const TONE = {
  neutral: { dot: "bg-navy", value: "text-ink" },
  success: { dot: "bg-success", value: "text-ink" },
  warning: { dot: "bg-warning", value: "text-ink" },
  danger: { dot: "bg-danger", value: "text-danger" },
  info: { dot: "bg-info", value: "text-ink" },
};

export default function StatCard({ label, value, tone = "neutral", to, hint, badge, sample = false }) {
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
      {/* The tag sits next to the number, not in a footnote. A figure a reader
          has already absorbed cannot be un-absorbed by a caption below it. */}
      <div className="mt-2 flex items-center gap-2">
        <p className={`text-3xl font-semibold ${t.value}`}>{value}</p>
        {sample && <SampleTag />}
      </div>
      {hint && <p className="mt-1 text-xs text-muted">{hint}</p>}
    </div>
  );
  return to ? <Link to={to} className="block h-full">{body}</Link> : body;
}
