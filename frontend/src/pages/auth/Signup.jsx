// ============================================================================
// Signup (/signup) — create an organisation and its first Admin/DPO.
//
// Registering creates a real tenant in PostgreSQL, isolated from every other
// tenant by row-level security, with its own audit chain starting at entry 1.
//
// Password feedback is shown as you type, but the SERVER is the authority — the
// same rules live in registration_service.py, and this form is a courtesy so you
// find out before you submit rather than after.
// ============================================================================
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  backendHealthy,
  checkWorkspace,
  passwordProblems,
  register,
  suggestWorkspace,
} from "../../api/auth";
import { useApp } from "../../context/AppContext";
import LanguageSwitcher from "../../components/common/LanguageSwitcher";

export default function Signup() {
  const { signIn } = useApp();
  const navigate = useNavigate();

  const [form, setForm] = useState({
    companyName: "",
    workspace: "",
    adminName: "",
    adminEmail: "",
    password: "",
  });
  // True once the user edits the workspace themselves, after which we stop
  // overwriting their choice with a suggestion.
  const [workspaceTouched, setWorkspaceTouched] = useState(false);
  const [availability, setAvailability] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [apiUp, setApiUp] = useState(null);

  useEffect(() => {
    backendHealthy().then(setApiUp);
  }, []);

  // Suggest a workspace id from the company name until the user takes over.
  useEffect(() => {
    if (!workspaceTouched) {
      setForm((f) => ({ ...f, workspace: suggestWorkspace(f.companyName) }));
    }
  }, [form.companyName, workspaceTouched]);

  // Debounced availability check — one request per pause, not per keystroke.
  useEffect(() => {
    const ws = form.workspace;
    if (!ws || ws.length < 2) {
      setAvailability(null);
      return undefined;
    }
    const id = setTimeout(() => {
      checkWorkspace(ws).then(setAvailability).catch(() => setAvailability(null));
    }, 400);
    return () => clearTimeout(id);
  }, [form.workspace]);

  const pwProblems = form.password
    ? passwordProblems(form.password, { email: form.adminEmail, name: form.adminName })
    : [];
  const canSubmit =
    form.companyName.trim().length >= 2 &&
    form.adminName.trim().length >= 2 &&
    form.adminEmail.includes("@") &&
    pwProblems.length === 0 &&
    availability?.available !== false;

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const session = await register(form);
      signIn(session);
      navigate("/admin/dashboard");
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

      <div className="flex flex-1 items-start justify-center px-4 pb-16 pt-4">
        <div className="w-full max-w-lg">
          <div className="mb-6 text-center">
            <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-navy text-xl text-white">
              🛡
            </div>
            <h1 className="text-xl font-semibold text-navy">Create your organisation</h1>
            <p className="mt-1 text-sm text-muted">
              You become the first Admin / Data Protection Officer.
            </p>
          </div>

          {apiUp === false && (
            <div className="mb-4 rounded-lg border border-danger/40 bg-danger/5 p-3 text-sm">
              <p className="font-medium text-ink">Cannot reach the API</p>
              <p className="mt-1 text-muted">
                Start it with <span className="mono">make api</span>, then reload.
              </p>
            </div>
          )}

          <form onSubmit={submit} className="card space-y-4 p-6">
            <div>
              <label className="label" htmlFor="company">Organisation name</label>
              <input
                id="company"
                className="input"
                value={form.companyName}
                onChange={(e) => setForm({ ...form, companyName: e.target.value })}
                placeholder="Acme Fintech Pvt. Ltd."
                autoComplete="organization"
                required
              />
              <p className="mt-1 text-xs text-muted">
                You are the Data Fiduciary. This name appears on consent notices.
              </p>
            </div>

            <div>
              <label className="label" htmlFor="ws">Workspace id</label>
              <input
                id="ws"
                className="input"
                value={form.workspace}
                onChange={(e) => {
                  setWorkspaceTouched(true);
                  setForm({ ...form, workspace: e.target.value.toLowerCase() });
                }}
                placeholder="acme-fintech"
                autoCapitalize="none"
                spellCheck="false"
                required
              />
              <div className="mt-1 flex items-center gap-2 text-xs">
                {availability === null && (
                  <span className="text-muted">Used at sign-in. Lowercase, no spaces.</span>
                )}
                {availability?.available === true && (
                  <span className="flex items-center gap-1.5 text-success">
                    <span className="h-1.5 w-1.5 rounded-full bg-success" aria-hidden="true" />
                    {availability.workspace} is available
                  </span>
                )}
                {availability?.available === false && (
                  <span className="flex items-center gap-1.5 text-danger">
                    <span className="h-1.5 w-1.5 rounded-full bg-danger" aria-hidden="true" />
                    {availability.reason}
                  </span>
                )}
              </div>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <label className="label" htmlFor="admin-name">Your name</label>
                <input
                  id="admin-name"
                  className="input"
                  value={form.adminName}
                  onChange={(e) => setForm({ ...form, adminName: e.target.value })}
                  autoComplete="name"
                  required
                />
              </div>
              <div>
                <label className="label" htmlFor="admin-email">Work email</label>
                <input
                  id="admin-email"
                  type="email"
                  className="input"
                  value={form.adminEmail}
                  onChange={(e) => setForm({ ...form, adminEmail: e.target.value })}
                  autoComplete="username"
                  required
                />
              </div>
            </div>

            <div>
              <label className="label" htmlFor="pw">Password</label>
              <input
                id="pw"
                type="password"
                className="input"
                value={form.password}
                onChange={(e) => setForm({ ...form, password: e.target.value })}
                autoComplete="new-password"
                required
                aria-describedby="pw-help"
              />
              <div id="pw-help" className="mt-1 text-xs">
                {form.password === "" && (
                  <span className="text-muted">
                    At least 12 characters. A few unrelated words beats a short complex
                    string.
                  </span>
                )}
                {form.password && pwProblems.length > 0 && (
                  <ul className="space-y-0.5">
                    {pwProblems.map((p) => (
                      <li key={p} className="flex items-center gap-1.5 text-danger">
                        <span className="h-1.5 w-1.5 rounded-full bg-danger" aria-hidden="true" />
                        {p}
                      </li>
                    ))}
                  </ul>
                )}
                {form.password && pwProblems.length === 0 && (
                  <span className="flex items-center gap-1.5 text-success">
                    <span className="h-1.5 w-1.5 rounded-full bg-success" aria-hidden="true" />
                    Strong enough
                  </span>
                )}
              </div>
            </div>

            {error && (
              <p className="flex items-start gap-2 rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-danger">
                <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-danger" aria-hidden="true" />
                {error}
              </p>
            )}

            <button type="submit" className="btn-primary w-full" disabled={busy || !canSubmit}>
              {busy ? "Creating…" : "Create organisation"}
            </button>

            <p className="text-center text-sm">
              <Link to="/login" className="text-teal underline">
                Already have an account? Sign in
              </Link>
            </p>
          </form>

          <div className="card mt-4 p-4 text-xs text-muted">
            <p className="font-medium text-ink">What happens when you submit</p>
            <ul className="mt-2 space-y-1">
              <li>• A tenant is created, isolated from every other by database row-level security.</li>
              <li>• You become its first Admin / DPO.</li>
              <li>• Your audit trail starts at entry 1 and is append-only from then on.</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  );
}
