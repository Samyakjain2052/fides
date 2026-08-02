// ============================================================================
// Routing, with role guards.
//
// The sidebar hides what a role can't use; these guards make that real, so
// typing /admin/roles as an Auditor lands you back on your own home screen
// instead of a screen you shouldn't see.
// ============================================================================
import { Link, Navigate, Route, Routes } from "react-router-dom";
import { ROLES } from "./api";
import { useApp } from "./context/AppContext";

import UserLayout from "./components/Layout/UserLayout";
import AdminLayout from "./components/Layout/AdminLayout";
import Toast from "./components/common/Toast";

import Login from "./pages/auth/Login";
import ForgotPassword from "./pages/auth/ForgotPassword";

import UserDashboard from "./pages/user/UserDashboard";
import ConsentBanner from "./pages/user/ConsentBanner";
import CookieConsent from "./pages/user/CookieConsent";
import PreferenceCentre from "./pages/user/PreferenceCentre";
import ConsentHistory from "./pages/user/ConsentHistory";
import DSARPortal from "./pages/user/DSARPortal";
import DSARStatus from "./pages/user/DSARStatus";
import GrievanceForm from "./pages/user/GrievanceForm";
import GrievanceStatus from "./pages/user/GrievanceStatus";

import AdminDashboard from "./pages/admin/AdminDashboard";
import DSARQueue from "./pages/admin/DSARQueue";
import ConsentQueue from "./pages/admin/ConsentQueue";
import GrievanceQueue from "./pages/admin/GrievanceQueue";
import BreachManagement from "./pages/admin/BreachManagement";
import AuditLogs from "./pages/admin/AuditLogs";
import UserRoleManagement from "./pages/admin/UserRoleManagement";
import DataRetentionPolicy from "./pages/admin/DataRetentionPolicy";
import NotificationCenter from "./pages/admin/NotificationCenter";
import Reports from "./pages/admin/Reports";

// Where each role goes when it lands on "/" or hits a screen it can't use.
function homeFor(role) {
  return ROLES.find((r) => r.id === role)?.home || "/login";
}

// Signed in at all?
function RequireAuth({ children }) {
  const { user } = useApp();
  if (!user) return <Navigate to="/login" replace />;
  return children;
}

// Signed in AND allowed on this screen.
function RequireRole({ allow, children }) {
  const { user } = useApp();
  if (!user) return <Navigate to="/login" replace />;
  if (!allow.includes(user.role)) return <Navigate to={homeFor(user.role)} replace />;
  return children;
}

export default function App() {
  const { user } = useApp();

  return (
    <Routes>
      {/* ---------------------------------------------------------- auth -- */}
      <Route path="/login" element={user ? <Navigate to={homeFor(user.role)} replace /> : <Login />} />
      <Route path="/forgot-password" element={<ForgotPassword />} />

      {/* Consent surfaces sit outside the app chrome: a first-time visitor sees
          these before they have an account, so they must not require auth. */}
      <Route path="/consent-banner" element={<ConsentBannerStandalone />} />
      <Route path="/cookie-consent" element={<CookieConsentStandalone />} />

      {/* ----------------------------------------------------- user side -- */}
      <Route
        path="/user"
        element={
          <RequireRole allow={["data_principal", "admin"]}>
            <UserLayout />
          </RequireRole>
        }
      >
        <Route index element={<Navigate to="/user/dashboard" replace />} />
        <Route path="dashboard" element={<UserDashboard />} />
        <Route path="preferences" element={<PreferenceCentre />} />
        <Route path="consent-history" element={<ConsentHistory />} />
        <Route path="dsar" element={<DSARPortal />} />
        <Route path="dsar/status" element={<DSARStatus />} />
        <Route path="grievance" element={<GrievanceForm />} />
        <Route path="grievance/status" element={<GrievanceStatus />} />
      </Route>

      {/* ---------------------------------------------------- admin side -- */}
      <Route
        path="/admin"
        element={
          <RequireAuth>
            <AdminLayout />
          </RequireAuth>
        }
      >
        <Route index element={<Navigate to="/admin/dashboard" replace />} />
        <Route path="dashboard" element={<RequireRole allow={["admin"]}><AdminDashboard /></RequireRole>} />
        <Route path="dsar" element={<RequireRole allow={["admin"]}><DSARQueue /></RequireRole>} />
        <Route path="consent-validation" element={<RequireRole allow={["admin"]}><ConsentQueue /></RequireRole>} />
        <Route
          path="grievances"
          element={
            <RequireRole allow={["admin", "grievance_officer"]}>
              <GrievanceQueue />
            </RequireRole>
          }
        />
        <Route path="breaches" element={<RequireRole allow={["admin"]}><BreachManagement /></RequireRole>} />
        <Route
          path="audit"
          element={
            <RequireRole allow={["admin", "auditor"]}>
              <AuditLogs />
            </RequireRole>
          }
        />
        <Route path="roles" element={<RequireRole allow={["admin"]}><UserRoleManagement /></RequireRole>} />
        <Route path="retention" element={<RequireRole allow={["admin"]}><DataRetentionPolicy /></RequireRole>} />
        <Route path="notifications" element={<RequireRole allow={["admin"]}><NotificationCenter /></RequireRole>} />
        <Route
          path="reports"
          element={
            <RequireRole allow={["admin", "auditor"]}>
              <Reports />
            </RequireRole>
          }
        />
      </Route>

      {/* --------------------------------------------------------- misc -- */}
      <Route path="/" element={<Navigate to={user ? homeFor(user.role) : "/login"} replace />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}

// The consent surfaces render WITHOUT the app chrome, at the top-level paths the
// brief specifies. That is faithful to when they actually appear: a first-time
// visitor meets the banner before they have an account, so it cannot sit inside a
// signed-in shell. When you are signed in, a link back to the app is added.
function StandaloneSurface({ children }) {
  const { user } = useApp();
  return (
    <div className="min-h-screen bg-canvas px-4 py-8">
      {user && (
        <div className="mx-auto mb-4 max-w-3xl">
          <Link to={homeFor(user.role)} className="text-sm text-teal underline">
            ← Back to the app
          </Link>
        </div>
      )}
      {children}
      <Toast />
    </div>
  );
}

function ConsentBannerStandalone() {
  return (
    <StandaloneSurface>
      <ConsentBanner />
    </StandaloneSurface>
  );
}

function CookieConsentStandalone() {
  return (
    <StandaloneSurface>
      <CookieConsent />
    </StandaloneSurface>
  );
}

function NotFound() {
  const { user } = useApp();
  return (
    <div className="flex min-h-screen items-center justify-center bg-canvas px-4">
      <div className="card max-w-md p-8 text-center">
        <p className="text-3xl font-semibold text-navy">404</p>
        <p className="mt-2 text-sm text-muted">That screen doesn&apos;t exist.</p>
        <a href={user ? homeFor(user.role) : "/login"} className="btn-primary mt-5">
          {user ? "Back to my dashboard" : "Go to sign in"}
        </a>
      </div>
    </div>
  );
}
