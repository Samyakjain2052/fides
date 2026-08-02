// ============================================================================
// Login (/login) — REAL authentication against the backend.
//
// The previous version was a demo: four hardcoded accounts, a role selector, and
// "any password is accepted". All of it is gone. This posts to
// /v1/auth/login and gets back a real JWT; the role comes from the database, not
// from a button the user picked.
//
// That last point matters. A client-side role selector is not authentication —
// it lets anyone choose to be an admin. The server now decides.
// ============================================================================
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { backendHealthy, login } from "../../api/auth";
import { useApp } from "../../context/AppContext";
import LanguageSwitcher from "../../components/common/LanguageSwitcher";

export default function Login() {
  const { signIn } = useApp();
  const navigate = useNavigate();

  const [form, setForm] = useState({ workspace: "", email: "", password: "" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [apiUp, setApiUp] = useState(null);

  // Say plainly that the API is down, rather than letting every sign-in fail
  // with a confusing network error.
  useEffect(() => {
    backendHealthy().then(setApiUp);
  }, []);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const session = await login({
        workspace: form.workspace.trim().toLowerCase(),
        email: form.email.trim(),
        password: form.password,
      });
      signIn(session);
      navigate(session.user.role === "data_principal" ? "/user/dashboard" : "/admin/dashboard");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen flex-col bg-canvas">
      <div className="flex justify-end p-4">
        <LanguageSwitcher />
      </div>

      <div className="flex flex-1 items-center justify-center px-4 pb-16">
        <div className="w-full max-w-md">
          <div className="mb-6 text-center">
            <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-navy text-xl text-white">
              🛡
            </div>
            <h1 className="text-xl font-semibold text-navy">DataShield</h1>
            <p className="text-sm text-muted">DPDP Compliance</p>
          </div>

          {apiUp === false && (
            <div className="mb-4 rounded-lg border border-danger/40 bg-danger/5 p-3 text-sm">
              <p className="flex items-center gap-2 font-medium text-ink">
                <span className="h-2 w-2 rounded-full bg-danger" aria-hidden="true" />
                Cannot reach the API
              </p>
              <p className="mt-1 text-muted">
                Start it with <span className="mono">make api</span>, then reload.
              </p>
            </div>
          )}

          <form onSubmit={submit} className="card space-y-4 p-6">
            <div>
              <label className="label" htmlFor="workspace">Workspace</label>
              <input
                id="workspace"
                className="input"
                value={form.workspace}
                onChange={(e) => setForm({ ...form, workspace: e.target.value })}
                placeholder="acme-fintech"
                autoComplete="organization"
                autoCapitalize="none"
                spellCheck="false"
                required
              />
              <p className="mt-1 text-xs text-muted">
                Your organisation&apos;s id. The same email can belong to more than one
                organisation, so we need to know which.
              </p>
            </div>

            <div>
              <label className="label" htmlFor="email">Email</label>
              <input
                id="email"
                type="email"
                className="input"
                value={form.email}
                onChange={(e) => setForm({ ...form, email: e.target.value })}
                autoComplete="username"
                required
              />
            </div>

            <div>
              <label className="label" htmlFor="password">Password</label>
              <input
                id="password"
                type="password"
                className="input"
                value={form.password}
                onChange={(e) => setForm({ ...form, password: e.target.value })}
                autoComplete="current-password"
                required
              />
            </div>

            {error && (
              <p className="flex items-start gap-2 rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-danger">
                <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-danger" aria-hidden="true" />
                {error}
              </p>
            )}

            <button type="submit" className="btn-primary w-full" disabled={busy}>
              {busy ? "Signing in…" : "Sign In"}
            </button>

            <div className="flex items-center justify-between text-sm">
              <Link to="/forgot-password" className="text-teal underline">
                Forgot password?
              </Link>
              <Link to="/signup" className="text-teal underline">
                Create an organisation
              </Link>
            </div>
          </form>

          <p className="mt-4 text-center text-xs text-muted">
            Your role and permissions come from your account — they are decided by the
            server on every request, not chosen here.
          </p>
        </div>
      </div>
    </div>
  );
}
