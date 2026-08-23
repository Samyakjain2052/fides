// ============================================================================
// UserLayout — sidebar + header for the Data Principal side.
// The language switcher and the notification bell live in the header, so both
// appear on every user-facing screen as the brief requires.
// ============================================================================
import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useApp } from "../../context/AppContext";
import { officer as fetchOfficer } from "../../api/grievances";
import LanguageSwitcher from "../common/LanguageSwitcher";
import NotificationBell from "../common/NotificationBell";
import Toast from "../common/Toast";
import PreviewBanner, { PreviewBadge } from "../common/PreviewBanner";
import { moduleForPath } from "../../config/modules";

const NAV = [
  { to: "/user/dashboard", label: "Dashboard", icon: "▦" },
  { to: "/user/preferences", label: "Preference Centre", icon: "☑", module: "consent" },
  { to: "/user/consent-history", label: "Consent History", icon: "⏱", module: "consent" },
  { to: "/user/dsar", label: "Data Requests", icon: "📋", module: "dsar" },
  { to: "/user/dsar/status", label: "Request Status", icon: "◷", module: "dsar" },
  { to: "/user/grievance", label: "File a Complaint", icon: "✉", module: "grievance" },
  { to: "/user/grievance/status", label: "Complaint Status", icon: "◔", module: "grievance" },
];

const DEMO = [
  { to: "/consent-banner", label: "Consent Banner" },
  { to: "/cookie-consent", label: "Cookie Banner" },
];

export default function UserLayout() {
  const { user, workspace, signOut, t } = useApp();

  // The Grievance Officer is a statutory contact under §13, so it is read from
  // the tenant rather than hardcoded. The previous version printed
  // "Amit Kumar · dpo@example.com" to every data principal in every workspace —
  // a fabricated contact for a right the Act requires be exercisable.
  const [officer, setOfficer] = useState(null);
  useEffect(() => {
    let live = true;
    fetchOfficer()
      .then((o) => live && setOfficer(o))
      .catch(() => live && setOfficer(null));
    return () => {
      live = false;
    };
  }, []);
  const navigate = useNavigate();
  const activeModule = moduleForPath(useLocation().pathname);

  const linkClass = ({ isActive }) =>
    `flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition ${
      isActive ? "bg-navy text-white font-medium" : "text-ink hover:bg-line/60"
    }`;

  return (
    <div className="flex min-h-screen bg-canvas">
      <aside className="hidden w-64 shrink-0 border-r border-line bg-surface lg:block">
        <div className="border-b border-line px-5 py-5">
          <p className="text-base font-semibold text-navy">DataShield</p>
          <p className="text-xs text-muted">DPDP Compliance</p>
        </div>
        <nav className="space-y-1 p-3">
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} className={linkClass}>
              <span aria-hidden="true" className="w-4 text-center">{item.icon}</span>
              <span className="min-w-0 flex-1 truncate">{t(item.label)}</span>
              <PreviewBadge module={item.module} />
            </NavLink>
          ))}
        </nav>
        <div className="mt-2 border-t border-line p-3">
          <p className="px-3 pb-2 text-xs font-semibold uppercase tracking-wide text-muted">
            Consent surfaces
          </p>
          {DEMO.map((item) => (
            <NavLink key={item.to} to={item.to} className={linkClass}>
              <span aria-hidden="true" className="w-4 text-center">◇</span>
              {item.label}
            </NavLink>
          ))}
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-line bg-surface px-5 py-3">
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold text-ink">
              {t("Welcome")}, {user?.full_name?.split(" ")[0] || "there"}
            </p>
            <p className="truncate text-xs text-muted">{workspace?.name || ""}</p>
          </div>
          <div className="flex items-center gap-2">
            <NavLink to="/roadmap" className="btn-ghost text-sm">What's live</NavLink>
            <LanguageSwitcher />
            <NotificationBell audience="user" />
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

        {/* Mobile nav */}
        <nav className="flex gap-2 overflow-x-auto border-b border-line bg-surface px-4 py-2 lg:hidden">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `whitespace-nowrap rounded-full px-3 py-1.5 text-xs ${
                  isActive ? "bg-navy text-white" : "bg-canvas text-ink"
                }`
              }
            >
              {t(item.label)}
              <PreviewBadge module={item.module} className="ml-1.5" />
            </NavLink>
          ))}
        </nav>

        <main className="mx-auto w-full max-w-6xl flex-1 space-y-5 px-4 py-6 sm:px-6">
          {activeModule && <PreviewBanner module={activeModule} />}
          <Outlet />
        </main>

        <footer className="border-t border-line bg-surface px-5 py-3 text-xs text-muted">
          <div className="flex flex-wrap items-center justify-between gap-2">
            {/* Says so plainly when none is published, rather than inventing
                one. An organisation without a published officer is a compliance
                gap the person is entitled to see. */}
            <span>
              {officer?.published
                ? `Grievance Officer: ${officer.name} · ${officer.email}`
                : "No Grievance Officer has been published for this organisation."}
            </span>
            <NavLink to="/cookie-consent" className="text-teal underline">
              Cookie Settings
            </NavLink>
          </div>
        </footer>
      </div>

      <Toast />
    </div>
  );
}
