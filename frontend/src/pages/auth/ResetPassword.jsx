// ============================================================================
// Reset Password (/reset-password?token=…)
//
// Where the emailed link lands. New, because there was no reset flow at all —
// the "forgot password" screen called a stub that returned success without
// sending anything, so no link existed to land anywhere.
//
// Signs the person in on success rather than sending them to /login. They have
// just proved control of the mailbox and chosen a password; a login form here
// would only be somewhere to mistype it.
//
// The password rules are checked client-side against the same helper the signup
// form uses, so somebody is not told their password is unacceptable only after
// submitting — but the server checks again, against this person's own name and
// email, which the browser has no business knowing before they are signed in.
// ============================================================================
import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { passwordProblems, resetPassword } from "../../api/auth";
import { useApp } from "../../context/AppContext";

export default function ResetPassword() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { signIn, notify } = useApp();

  const token = params.get("token") || "";
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // Said immediately, not on submit. A link opened without a token is usually a
  // truncated one from an email client, and finding that out after typing a
  // password twice is a poor way to learn it.
  useEffect(() => {
    if (!token) {
      setError(
        "This link is missing its token. Email clients sometimes cut long " +
          "links — try opening it again from the email, or ask for a new one.",
      );
    }
  }, [token]);

  const problems = password ? passwordProblems(password) : [];
  const mismatch = again.length > 0 && password !== again;
  const canSubmit =
    Boolean(token) && password.length > 0 && problems.length === 0 && !mismatch;

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const session = await resetPassword({ token, password });
      signIn(session);
      notify("Your password has been changed. Every other session was signed out.");
      navigate(
        session.user.role === "data_principal"
          ? "/user/dashboard"
          : "/admin/dashboard",
      );
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-canvas px-4">
      <div className="w-full max-w-md">
        <div className="mb-6 text-center">
          <h1 className="text-xl font-semibold text-navy">Choose a new password</h1>
          <p className="text-sm text-muted">
            Setting it signs you in, and signs out every other session.
          </p>
        </div>

        <form onSubmit={submit} className="card space-y-4 p-6">
          <div>
            <label className="label" htmlFor="new-password">New password</label>
            <input
              id="new-password"
              type="password"
              className="input"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
              disabled={!token}
              required
            />
            {password && problems.length === 0 && (
              <p className="mt-1 text-xs text-success">That will do.</p>
            )}
            {problems.map((p) => (
              <p key={p} className="mt-1 text-xs text-danger">{p}</p>
            ))}
          </div>

          <div>
            <label className="label" htmlFor="new-password-again">
              New password again
            </label>
            <input
              id="new-password-again"
              type="password"
              className="input"
              value={again}
              onChange={(e) => setAgain(e.target.value)}
              autoComplete="new-password"
              disabled={!token}
              required
            />
            {mismatch && (
              <p className="mt-1 text-xs text-danger">
                These two do not match.
              </p>
            )}
          </div>

          {error && (
            <p className="rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-danger">
              {error}
            </p>
          )}

          <button
            type="submit"
            className="btn-primary w-full"
            disabled={busy || !canSubmit}
          >
            {busy ? "Setting…" : "Set my password"}
          </button>

          <p className="text-center text-sm text-muted">
            <Link to="/forgot-password" className="text-teal underline">
              Ask for a new link
            </Link>
          </p>
        </form>
      </div>
    </div>
  );
}
