// ============================================================================
// NotificationBell — the last few messages, from the real delivery log.
//
// Two audiences, two endpoints, and deliberately not the same data:
//
//   * "user"      → /notifications/mine — what this platform told *me*.
//   * "fiduciary" → /notifications/log  — what it told everyone, for a DPO.
//
// The previous version had an unread count derived from `status !== "read"`,
// against rows whose status was never "read". It therefore showed every message
// as unread, forever. There is no read-receipt in this product, so the badge now
// counts what actually warrants attention on each side: failures for an operator,
// nothing for a data principal.
// ============================================================================
import { useEffect, useRef, useState } from "react";
import { deliveryLog, myNotifications } from "../../api/notifications";
import StatusBadge from "./StatusBadge";

export default function NotificationBell({ audience = "user" }) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState([]);
  const boxRef = useRef(null);

  useEffect(() => {
    const load =
      audience === "fiduciary"
        ? () => deliveryLog({ limit: 10 })
        : () => myNotifications();
    // A bell is decoration; a failure to load one must not surface an error over
    // the page the person actually came for.
    load()
      .then((rows) => setItems(rows.slice(0, 10)))
      .catch(() => setItems([]));
  }, [audience]);

  useEffect(() => {
    const onClick = (e) => {
      if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  // What an operator needs to act on. A data principal has nothing to act on
  // here, so they get no badge rather than a number that means nothing.
  const needsAttention =
    audience === "fiduciary" ? items.filter((n) => n.status === "failed").length : 0;

  return (
    <div className="relative" ref={boxRef}>
      <button
        type="button"
        className="relative rounded-lg p-2 hover:bg-line/60"
        onClick={() => setOpen((v) => !v)}
        aria-label={
          needsAttention
            ? `Notifications, ${needsAttention} failed to send`
            : "Notifications"
        }
      >
        <span aria-hidden="true">🔔</span>
        {needsAttention > 0 && (
          <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-danger px-1 text-[10px] font-semibold text-white">
            {needsAttention}
          </span>
        )}
      </button>

      {open && (
        <div className="card absolute right-0 z-40 mt-2 w-80 p-0 shadow-panel">
          <div className="border-b border-line px-4 py-3">
            <p className="text-sm font-semibold">
              {audience === "fiduciary" ? "Recent messages sent" : "Messages about your data"}
            </p>
          </div>
          <ul className="max-h-80 divide-y divide-line overflow-y-auto">
            {items.length === 0 && (
              <li className="px-4 py-6 text-center text-sm text-muted">Nothing yet.</li>
            )}
            {items.map((n) => (
              <li key={n.id} className="px-4 py-3">
                <p className="text-sm text-ink">{n.subject_rendered}</p>
                <div className="mt-1.5 flex items-center gap-2 text-xs text-muted">
                  <StatusBadge status={n.status} />
                  <span>{n.channel}</span>
                  <span>·</span>
                  <span>{new Date(n.sent_at || n.queued_at).toLocaleDateString()}</span>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
