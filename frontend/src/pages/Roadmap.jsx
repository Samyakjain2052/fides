// ============================================================================
// /roadmap — what is live, what is not, and roughly when.
//
// Public on purpose. It is linked from every preview banner, and a prospective
// buyer should be able to read it before they have an account. An honest
// roadmap is a better artifact in a sales conversation than a thin demo that
// implies more than it does.
// ============================================================================
import { Link } from "react-router-dom";
import {
  MODULE_CAVEATS,
  MODULE_LABELS,
  MODULE_ROADMAP,
  SHIP_TARGET,
  liveModules,
  previewModules,
} from "../config/modules";
import { useApp } from "../context/AppContext";

const BUILD_SHA = import.meta.env.VITE_BUILD_SHA || "dev";

export default function Roadmap() {
  const { user } = useApp();
  const live = liveModules();
  const preview = previewModules();

  return (
    <div className="min-h-screen bg-canvas px-4 py-10">
      <div className="mx-auto max-w-3xl space-y-6">
        <header>
          <p className="text-sm font-semibold text-navy">DataShield</p>
          <h1 className="mt-1 text-2xl font-semibold text-ink">
            What works today, and what does not
          </h1>
          <p className="mt-2 text-sm text-muted">
            This is a pilot environment. Rather than let you find the edges by
            trial and error, here is the whole picture: {live.length} module
            {live.length === 1 ? "" : "s"} running on real infrastructure,{" "}
            {preview.length} still on sample data.
          </p>
        </header>

        {/* ------------------------------------------------------------ live -- */}
        <section className="card overflow-hidden">
          <div className="flex items-center gap-2 border-b border-line px-5 py-4">
            <span className="h-2 w-2 rounded-full bg-success" aria-hidden="true" />
            <h2 className="font-semibold text-ink">Live now</h2>
          </div>
          <ul className="divide-y divide-line">
            {live.map((key) => (
              <li key={key} className="px-5 py-4">
                <div className="flex flex-wrap items-center gap-2">
                  <p className="font-medium text-ink">{MODULE_LABELS[key]}</p>
                  <span className="rounded-full bg-success px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-white">
                    Live
                  </span>
                </div>
                <p className="mt-1 text-sm text-muted">{LIVE_DETAIL[key]}</p>
                {MODULE_CAVEATS[key] && (
                  <p className="mt-2 rounded border border-info/40 bg-info/10 px-3 py-2 text-sm text-ink">
                    <strong className="font-semibold">One exception:</strong>{" "}
                    {MODULE_CAVEATS[key]}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </section>

        {/* --------------------------------------------------------- preview -- */}
        <section className="card overflow-hidden">
          <div className="flex items-center gap-2 border-b border-line px-5 py-4">
            <span className="h-2 w-2 rounded-full bg-warning" aria-hidden="true" />
            <h2 className="font-semibold text-ink">
              Preview — built, on sample data
            </h2>
          </div>
          <div className="border-b border-line bg-canvas px-5 py-3">
            <p className="text-sm text-muted">
              You can open these screens and click through them. The data is
              illustrative and the controls that would change something are
              disabled. Target: <strong className="text-ink">{SHIP_TARGET}</strong>.
            </p>
          </div>
          <ul className="divide-y divide-line">
            {preview.map((key) => (
              <li key={key} className="px-5 py-4">
                <div className="flex flex-wrap items-center gap-2">
                  <p className="font-medium text-ink">{MODULE_LABELS[key]}</p>
                  <span className="rounded-full border border-warning/60 bg-warning/15 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-warning">
                    Preview
                  </span>
                </div>
                <p className="mt-1 text-sm text-muted">
                  <span className="text-ink">Needed before it is live: </span>
                  {MODULE_ROADMAP[key]}
                </p>
              </li>
            ))}
          </ul>
        </section>

        {/* ------------------------------------------------------------ note -- */}
        <section className="card p-5">
          <h2 className="font-semibold text-ink">Why show this at all</h2>
          <p className="mt-2 text-sm text-muted">
            We sell a compliance product. A demo that overstates what it does is
            the wrong first impression from a company asking you to trust it with
            personal data. Everything above is generated from a single file in the
            codebase, so what you read here is what the application enforces —
            preview modules have their controls disabled by that same file.
          </p>
        </section>

        <footer className="flex flex-wrap items-center justify-between gap-3 pt-2">
          <Link to={user ? "/" : "/login"} className="btn-secondary text-sm">
            {user ? "Back to the app" : "Go to sign in"}
          </Link>
          <p className="font-mono text-xs text-muted">build {BUILD_SHA}</p>
        </footer>
      </div>
    </div>
  );
}

const LIVE_DETAIL = {
  auth:
    "Create an organisation, sign in, and manage roles. Passwords are Argon2id, " +
    "sessions are short-lived tokens with rotating refresh cookies, and one " +
    "tenant cannot read another's rows — enforced by database row-level " +
    "security, not by application code.",
  dsar:
    "Submit an access or erasure request and it executes for real: one request " +
    "fans out across PostgreSQL, MongoDB, MySQL and Zoho CRM, returns the data " +
    "it found, and on erasure masks the identifying fields in every one of them.",
};
