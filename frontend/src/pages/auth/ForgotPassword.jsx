// ============================================================================
// Forgot Password (/forgot-password) — email input, send link, confirmation.
// ============================================================================
import { useState } from "react";
import { Link } from "react-router-dom";
import { sendResetLink } from "../../api";

export default function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await sendResetLink(email);
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
              If <strong className="text-ink">{email}</strong> is registered with us, a reset link
              is on its way. The link expires in 30 minutes.
            </p>
            <Link to="/login" className="btn-secondary mt-5 w-full">
              Back to sign in
            </Link>
          </div>
        ) : (
          <form onSubmit={submit} className="card space-y-4 p-6">
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
