// ============================================================================
// Login (/login) — centred card, email + password, role selector.
// On success the user lands on the home route for their role.
// ============================================================================
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { login, ROLES } from "../../api";
import { useApp } from "../../context/AppContext";
import LanguageSwitcher from "../../components/common/LanguageSwitcher";

export default function Login() {
  const { setUser } = useApp();
  const navigate = useNavigate();
  const [form, setForm] = useState({
    email: "priya@example.com",
    password: "demo1234",
    role: "data_principal",
  });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const profile = await login(form);
      setUser(profile);
      navigate(ROLES.find((r) => r.id === form.role)?.home || "/user/dashboard");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  // Switching role swaps in that role's demo account, so the four sides of the
  // product are one click apart.
  const pickRole = (role) => {
    const demoEmail = {
      data_principal: "priya@example.com",
      admin: "amit@example.com",
      auditor: "ravi@example.com",
      grievance_officer: "meena@example.com",
    }[role];
    setForm((f) => ({ ...f, role, email: demoEmail || f.email }));
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

          <form onSubmit={submit} className="card space-y-4 p-6">
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

            <fieldset>
              <legend className="label">Sign in as</legend>
              <div className="grid grid-cols-2 gap-2">
                {ROLES.map((r) => (
                  <button
                    key={r.id}
                    type="button"
                    onClick={() => pickRole(r.id)}
                    className={`rounded-lg border px-3 py-2 text-sm transition ${
                      form.role === r.id
                        ? "border-navy bg-navy/5 font-medium text-navy"
                        : "border-line text-ink hover:bg-line/40"
                    }`}
                    aria-pressed={form.role === r.id}
                  >
                    {r.label}
                  </button>
                ))}
              </div>
            </fieldset>

            {error && (
              <p className="flex items-center gap-2 rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-danger">
                <span className="h-2 w-2 rounded-full bg-danger" aria-hidden="true" />
                {error}
              </p>
            )}

            <button type="submit" className="btn-primary w-full" disabled={busy}>
              {busy ? "Signing in…" : "Sign In"}
            </button>

            <div className="text-center">
              <Link to="/forgot-password" className="text-sm text-teal underline">
                Forgot password?
              </Link>
            </div>
          </form>

          <p className="mt-4 text-center text-xs text-muted">
            Demo build — any password is accepted. Pick a role to see that side of the product.
          </p>
        </div>
      </div>
    </div>
  );
}
