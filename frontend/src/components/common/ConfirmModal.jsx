// ============================================================================
// ConfirmModal — required in front of EVERY destructive action (withdraw
// consent, erase data, delete a policy, revoke access).
//
// `consequences` is the important part: the brief wants the user told what they
// will lose, not just asked "are you sure?".
// ============================================================================
import { useEffect, useRef } from "react";

export default function ConfirmModal({
  open,
  title,
  body,
  consequences = [],
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  destructive = true,
  busy = false,
  onConfirm,
  onCancel,
}) {
  const confirmRef = useRef(null);

  // Keyboard: Escape closes, focus lands on the confirm button.
  useEffect(() => {
    if (!open) return undefined;
    confirmRef.current?.focus();
    const onKey = (e) => {
      if (e.key === "Escape") onCancel?.();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onCancel]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-title"
      onClick={(e) => e.target === e.currentTarget && onCancel?.()}
    >
      <div className="card w-full max-w-md p-6 shadow-panel">
        <h2 id="confirm-title" className="text-lg font-semibold text-ink">
          {title}
        </h2>
        {body && <p className="mt-2 text-sm text-muted">{body}</p>}

        {consequences.length > 0 && (
          <div className="mt-4 rounded-lg border border-line bg-canvas p-3">
            <p className="text-xs font-semibold uppercase tracking-wide text-muted">
              What this means
            </p>
            <ul className="mt-2 space-y-1.5 text-sm text-ink">
              {consequences.map((c) => (
                <li key={c} className="flex gap-2">
                  <span
                    className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${destructive ? "bg-danger" : "bg-navy"}`}
                    aria-hidden="true"
                  />
                  <span>{c}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="mt-6 flex justify-end gap-3">
          <button type="button" className="btn-ghost" onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </button>
          <button
            ref={confirmRef}
            type="button"
            className={destructive ? "btn bg-danger text-white hover:bg-danger/90" : "btn-primary"}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
