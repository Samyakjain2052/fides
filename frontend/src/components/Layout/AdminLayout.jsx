// ============================================================================
// AdminLayout — sidebar + header for the compliance side.
//
// Navigation is filtered by role, which is how the brief's role model is
// actually enforced in the UI:
//   admin              → everything
//   auditor            → read-only: audit logs + reports
//   grievance_officer  → the grievance queue only
// Route-level guards in App.jsx back this up, so typing a URL doesn't bypass it.
// ============================================================================
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useApp } from "../../context/AppContext";
import LanguageSwitcher from "../common/LanguageSwitcher";
import NotificationBell from "../common/NotificationBell";
import Toast from "../common/Toast";

const NAV = [
  { to: "/admin/dashboard", label: "Dashboard", icon: "▦", roles: ["admin"] },
  { to: "/admin/dsar", label: "DSAR Queue", icon: "📋", roles: ["admin"] },
  { to: "/admin/consent-validation", label: "Consent Validation", icon: "✓", roles: ["admin"] },
  { to: "/admin/grievances", label: "Grievance Queue", icon: "✉", roles: ["admin", "grievance_officer"] },
  { to: "/admin/breaches", label: "Breach Management", icon: "⚠", roles: ["admin"] },
  { to: "/admin/audit", label: "Audit Logs", icon: "🔒", roles: ["admin", "auditor"] },
  { to: "/admin/roles", label: "Users & Roles", icon: "👥", roles: ["admin"] },
  { to: "/admin/retention", label: "Retention Policy", icon: "🗓", roles: ["admin"] },
  { to: "/admin/notifications", label: "Notification Center", icon: "🔔", roles: ["admin"] },
  { to: "/admin/reports", label: "Reports", icon: "📄", roles: ["admin", "auditor"] },
];

export function navFor(role) {
  return NAV.filter((item) => item.roles.includes(role));
}

export default function AdminLayout() {
  const { user, role, roleLabel, signOut } = useApp();
  const navigate = useNavigate();
  const items = navFor(role);

  const linkClass = ({ isActive }) =>
    `flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition ${
      isActive ? "bg-navy text-white font-medium" : "text-ink hover:bg-line/60"
    }`;

  return (
    <div className="flex min-h-screen bg-canvas">
      <aside className="hidden w-64 shrink-0 border-r border-line bg-surface lg:block">
        <div className="border-b border-line px-5 py-5">
          <p className="text-base font-semibold text-navy">DataShield</p>
          <p className="text-xs text-muted">Compliance Console</p>
        </div>
        <nav className="space-y-1 p-3">
          {items.map((item) => (
            <NavLink key={item.to} to={item.to} className={linkClass}>
              <span aria-hidden="true" className="w-4 text-center">{item.icon}</span>
              {item.label}
            </NavLink>
          ))}
        </nav>
        {role === "auditor" && (
          <p className="mx-3 rounded-lg border border-line bg-canvas p-3 text-xs text-muted">
            You are signed in as an <strong className="text-ink">Auditor</strong> — read-only
            access to the audit trail and reports.
          </p>
        )}
        {role === "grievance_officer" && (
          <p className="mx-3 rounded-lg border border-line bg-canvas p-3 text-xs text-muted">
            You are signed in as a <strong className="text-ink">Grievance Officer</strong> — the
            grievance queue only.
          </p>
        )}
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-line bg-surface px-5 py-3">
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold text-ink">{user?.name}</p>
            <p className="truncate text-xs text-muted">{roleLabel} · {user?.email}</p>
          </div>
          <div className="flex items-center gap-2">
            <LanguageSwitcher compact />
            <NotificationBell audience="fiduciary" />
            <button
              type="button"
              className="btn-ghost text-sm"
              onClick={async () => {
                // Await it: signOut revokes the refresh-token family server-side,
                // and navigating first would race that call.
                await signOut();
                navigate("/login");
              }}
            >
              Sign out
            </button>
          </div>
        </header>

        <nav className="flex gap-2 overflow-x-auto border-b border-line bg-surface px-4 py-2 lg:hidden">
          {items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `whitespace-nowrap rounded-full px-3 py-1.5 text-xs ${
                  isActive ? "bg-navy text-white" : "bg-canvas text-ink"
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>

        <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 sm:px-6">
          <Outlet />
        </main>
      </div>

      <Toast />
    </div>
  );
}
