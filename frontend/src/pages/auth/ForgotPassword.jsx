// ============================================================================
// Forgot Password (/forgot-password)
//
// Real, against /v1/auth/forgot-password. It used to call a stub that waited
// 500ms and returned success without a network call, so this screen showed
// "check your inbox" and nothing was ever sent.
//
// It asks for the WORKSPACE as well as the email, because an address can exist
// in more than one workspace and the server needs to know which account to reset
// — the same reason sign-in asks for it.
//
// The confirmation is worded to promise nothing. The server answers identically
// whether or not the address has an account, because "does this person have an
// account with this company" is itself personal data, so this screen must not
// assert that an email is on its way.
// ============================================================================
import { useState } from "react";
import { Link } from "react-router-dom";
import { requestPasswordReset } from "../../api/auth";

export default function ForgotPassword() {
  const [workspace, setWorkspace] = useState("");
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await requestPasswordReset({ workspace, email });
      setSent(true);
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
          <h1 className="text-xl font-semibold text-navy">Reset your password</h1>
          <p className="text-sm text-muted">
            We&apos;ll email you a link to choose a new one.
          </p>
        </div>

        {sent ? (
          <div className="card p-6 text-center">
            <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-success/10 text-2xl text-success">
              ✓
            </div>
            <h2 className="font-semibold text-ink">Check your inbox</h2>
            <p className="mt-2 text-sm text-muted">
              If <strong className="text-ink">{email}</strong> has an account in{" "}
              <strong className="text-ink">{workspace}</strong>, a reset link is on
              its way. It works once and expires in an hour.
            </p>
            <p className="mt-2 text-xs text-muted">
              We do not confirm whether an address is registered — that would let
              anybody check who uses this workspace.
            </p>
            <Link to="/login" className="btn-secondary mt-5 w-full">
              Back to sign in
            </Link>
          </div>
        ) : (
          <form onSubmit={submit} className="card space-y-4 p-6">
            <div>
              <label className="label" htmlFor="reset-workspace">Workspace</label>
              <input
                id="reset-workspace"
                className="input"
                value={workspace}
                onChange={(e) => setWorkspace(e.target.value)}
                placeholder="acme-fintech"
                autoCapitalize="none"
                spellCheck="false"
                required
              />
              <p className="mt-1 text-xs text-muted">
                The same workspace id you use to sign in.
              </p>
            </div>

            <div>
              <label className="label" htmlFor="reset-email">Registered email</label>
              <input
                id="reset-email"
                type="email"
                className="input"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                required
              />
            </div>

            {error && (
              <p className="flex items-center gap-2 rounded-lg border border-danger/40 bg-danger/5 px-3 py-2 text-sm text-danger">
                <span className="h-2 w-2 rounded-full bg-danger" aria-hidden="true" />
                {error}
              </p>
            )}

            <button type="submit" className="btn-primary w-full" disabled={busy}>
              {busy ? "Sending…" : "Send Reset Link"}
            </button>
            <div className="text-center">
              <Link to="/login" className="text-sm text-teal underline">
                Back to sign in
              </Link>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
