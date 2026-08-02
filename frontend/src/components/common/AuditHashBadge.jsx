// ============================================================================
// AuditHashBadge — the tamper-evidence indicator on an audit row.
// Shows a truncated hash; click to copy the full value.
// ============================================================================
import { useState } from "react";

export default function AuditHashBadge({ hash, chars = 14 }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(hash);
    } catch {
      /* clipboard blocked (non-https / permissions) — the title still shows it */
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <button
      type="button"
      onClick={copy}
      title={`${hash}\nClick to copy`}
      className="inline-flex items-center gap-1.5 rounded-full border border-line bg-canvas px-2 py-0.5 font-mono text-[11px] text-muted hover:bg-line/60"
    >
      <span aria-hidden="true">🔒</span>
      <span>{copied ? "copied" : hash.slice(0, chars) + "…"}</span>
    </button>
  );
}
