// ============================================================================
// NotificationBell — bell + unread count, opens the last 10 notifications.
// ============================================================================
import { useEffect, useRef, useState } from "react";
import { getNotifications } from "../../api";
import StatusBadge from "./StatusBadge";

export default function NotificationBell({ audience = "user" }) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState([]);
  const [seen, setSeen] = useState(false);
  const boxRef = useRef(null);

  useEffect(() => {
    getNotifications(audience).then((rows) => setItems(rows.slice(0, 10)));
  }, [audience]);

  useEffect(() => {
    const onClick = (e) => {
      if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  const unread = seen ? 0 : items.filter((n) => n.status !== "read").length;

  return (
    <div className="relative" ref={boxRef}>
      <button
        type="button"
        className="relative rounded-lg p-2 hover:bg-line/60"
        onClick={() => {
          setOpen((v) => !v);
          setSeen(true);
        }}
        aria-label={`Notifications${unread ? `, ${unread} unread` : ""}`}
      >
        <span aria-hidden="true">🔔</span>
        {unread > 0 && (
          <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-danger px-1 text-[10px] font-semibold text-white">
            {unread}
          </span>
        )}
      </button>

      {open && (
        <div className="card absolute right-0 z-40 mt-2 w-80 p-0 shadow-panel">
          <div className="border-b border-line px-4 py-3">
            <p className="text-sm font-semibold">Notifications</p>
          </div>
          <ul className="max-h-80 divide-y divide-line overflow-y-auto">
            {items.length === 0 && (
              <li className="px-4 py-6 text-center text-sm text-muted">Nothing yet.</li>
            )}
            {items.map((n) => (
              <li key={n.id} className="px-4 py-3">
                <p className="text-sm text-ink">{n.subject}</p>
                <div className="mt-1.5 flex items-center gap-2 text-xs text-muted">
                  <StatusBadge status={n.status} />
                  <span>{n.channel}</span>
                  <span>·</span>
                  <span>{new Date(n.sent_at).toLocaleDateString()}</span>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
