// ============================================================================
// SlideOver — the right-hand detail panel used by the DSAR and grievance
// queues ("detail slide-in panel, opens on row click").
// ============================================================================
import { useEffect } from "react";

export default function SlideOver({ open, title, subtitle, onClose, children, footer }) {
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => e.key === "Escape" && onClose?.();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-ink/30" role="dialog" aria-modal="true"
         onClick={(e) => e.target === e.currentTarget && onClose?.()}>
      <aside className="flex h-full w-full max-w-xl flex-col bg-surface shadow-panel">
        <header className="flex items-start justify-between gap-4 border-b border-line px-6 py-4">
          <div>
            <h2 className="text-lg font-semibold text-ink">{title}</h2>
            {subtitle && <p className="mt-0.5 text-sm text-muted">{subtitle}</p>}
          </div>
          <button type="button" className="btn-ghost px-2" onClick={onClose} aria-label="Close panel">
            ✕
          </button>
        </header>
        <div className="flex-1 overflow-y-auto px-6 py-5">{children}</div>
        {footer && <footer className="border-t border-line px-6 py-4">{footer}</footer>}
      </aside>
    </div>
  );
}
