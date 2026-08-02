// ============================================================================
// Toast — the success/failure confirmation the brief asks for after a state
// change ("success toast + audit log entry created"). Driven by AppContext.
// ============================================================================
import { useApp } from "../../context/AppContext";

export default function Toast() {
  const { toast } = useApp();
  if (!toast) return null;

  const tone =
    toast.tone === "error"
      ? { dot: "bg-danger", ring: "border-danger/40" }
      : toast.tone === "info"
        ? { dot: "bg-info", ring: "border-info/40" }
        : { dot: "bg-success", ring: "border-success/40" };

  return (
    <div
      role="status"
      aria-live="polite"
      className={`fixed bottom-6 left-1/2 z-50 -translate-x-1/2 rounded-xl border bg-surface px-4 py-3 shadow-panel ${tone.ring}`}
    >
      <div className="flex items-center gap-2.5">
        <span className={`h-2 w-2 rounded-full ${tone.dot}`} aria-hidden="true" />
        <span className="text-sm text-ink">{toast.message}</span>
      </div>
    </div>
  );
}
