// ============================================================================
// StatusBadge — a coloured dot AND a text label, always. Never colour alone:
// that is both an accessibility rule and a stated requirement of the brief.
// ============================================================================
const VARIANTS = {
  active: { dot: "bg-success", text: "Active" },
  valid: { dot: "bg-success", text: "Valid" },
  resolved: { dot: "bg-success", text: "Resolved" },
  completed: { dot: "bg-success", text: "Completed" },
  delivered: { dot: "bg-success", text: "Delivered" },

  pending: { dot: "bg-warning", text: "Pending" },
  expiring: { dot: "bg-warning", text: "Expiring soon" },
  acknowledged: { dot: "bg-warning", text: "Acknowledged" },
  investigating: { dot: "bg-warning", text: "Investigating" },

  in_progress: { dot: "bg-info", text: "In progress" },
  open: { dot: "bg-info", text: "Open" },
  submitted: { dot: "bg-info", text: "Submitted" },
  verified: { dot: "bg-info", text: "Verified" },

  withdrawn: { dot: "bg-danger", text: "Withdrawn" },
  expired: { dot: "bg-danger", text: "Expired" },
  rejected: { dot: "bg-danger", text: "Rejected" },
  failed: { dot: "bg-danger", text: "Failed" },
  escalated: { dot: "bg-danger", text: "Escalated" },
  // Overdue is the one variant that animates, because it is the one the brief
  // singles out as needing to grab a compliance officer's eye.
  overdue: { dot: "bg-danger animate-pulse", text: "Overdue" },

  reported_to_dpb: { dot: "bg-success", text: "Reported to DPB" },
  none: { dot: "bg-muted", text: "None" },
};

export default function StatusBadge({ status, label, className = "" }) {
  const key = String(status || "none").toLowerCase();
  const variant = VARIANTS[key] || { dot: "bg-muted", text: key.replace(/_/g, " ") };
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border border-line bg-canvas px-2.5 py-1 text-xs font-medium text-ink ${className}`}
    >
      <span className={`h-2 w-2 shrink-0 rounded-full ${variant.dot}`} aria-hidden="true" />
      {label || variant.text}
    </span>
  );
}
