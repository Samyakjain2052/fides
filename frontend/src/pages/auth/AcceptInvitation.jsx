// ============================================================================
// Accept an invitation (/accept-invitation?token=…)
//
// Public. The person arriving here has no account yet and no session.
//
// They set their own password, which is the entire point: nobody at the
// organisation that invited them — including whoever sent the invitation — ever
// knows or can see it. That is what keeps the audit trail meaningful, because an
// entry attributed to this person is one only this person could have produced.
//
// The failure message is deliberately the same for every reason a link can be
// invalid — expired, already used, withdrawn, or simply wrong. Anything more
// specific would let somebody probe which invitations exist in a workspace they
// have nothing to do with.
// ============================================================================
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { acceptInvitation } from "../../api/users";
import { passwordProblems } from "../../api/auth";
import { useApp } from "../../context/AppContext";

/** Where each role lands. Mirrors the router's own mapping. */
const HOME_FOR = {
  admin: "/admin/dashboard",
  auditor: "/admin/audit",
  grievance_officer: "/admin/grievances",
  data_principal: "/user/dashboard",
};

export default function AcceptInvitation() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { signIn } = useApp();

  const token = params.get("token") || "";
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // Mirrors the server's policy so the form can respond as you type. The SERVER
  // is the authority — this is a courtesy, and the same function the signup form
  // uses, so the two cannot drift apart.
  const problems = useMemo(
    () => passwordProblems(password, { email: "", name: fullName }),
    [password, fullName],
  );
  const mismatch = confirm.length > 0 && confirm !== password;
  const ready =
    token && fullName.trim().length >= 2 && problems.length === 0 && !mismatch &&
    confirm === password;

  useEffect(() => {
    if (!token) setError("This link is missing its invitation code.");
  }, [token]);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const session = await acceptInvitation({
        token, fullName: fullName.trim(), password,
      });
      // The server signs them in on acceptance, so there is no second login
      // step. Hand the session to the context the same way sign-in does — the
      // API layer's identity has to be set before any routed screen mounts.
      signIn(session);
      navigate(HOME_FOR[session.user.role] || "/user/dashboard", { replace: true });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto max-w-md space-y-5 py-10">
      <div>
        <h1 className="text-xl font-semibold text-ink">Set up your account</h1>
        <p className="mt-1 text-sm text-muted">
          You have been invited to a DataShield workspace. Choose a password —
          nobody at the organisation that invited you sets it, and nobody can see
          it.
        </p>
      </div>

      <form onSubmit={submit} className="card space-y-4 p-5">
        <div>
          <label className="label" htmlFor="ai-name">Your name</label>
          <input
            id="ai-name"
            className="input"
            value={fullName}
            onChange={(e) => setFullName(e.target.value)}
            autoComplete="name"
          />
          <p className="mt-1 text-xs text-muted">
            This is what appears next to your actions in the audit trail.
          </p>
        </div>

        <div>
          <label className="label" htmlFor="ai-password">Password</label>
          <input
            id="ai-password"
            className="input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
            aria-describedby="ai-pw-help"
          />
          <div id="ai-pw-help" className="mt-1 space-y-0.5 text-xs">
            {password.length === 0 ? (
              <p className="text-muted">
                At least 12 characters. A few unrelated words beats a short complex
                string.
              </p>
            ) : problems.length === 0 ? (
              <p className="text-success">That will do.</p>
            ) : (
              problems.map((p) => (
                <p key={p} className="text-danger">
                  {p}
                </p>
              ))
            )}
          </div>
        </div>

        <div>
          <label className="label" htmlFor="ai-confirm">Password again</label>
          <input
            id="ai-confirm"
            className="input"
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
          />
          {mismatch && (
            <p className="mt-1 text-xs text-danger">Those do not match.</p>
          )}
        </div>

        {error && (
          <div className="rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-danger">
            <p>{error}</p>
            <p className="mt-1 text-xs text-muted">
              If the link has expired or already been used, ask whoever invited you
              to send a new one.
            </p>
          </div>
        )}

        <button type="submit" className="btn-primary w-full" disabled={busy || !ready}>
          {busy ? "Setting up…" : "Create my account"}
        </button>

        <p className="text-center text-xs text-muted">
          Already have an account? <Link to="/login" className="text-teal underline">Sign in</Link>
        </p>
      </form>
    </div>
  );
}
