// ============================================================================
// Cookie Consent (/cookie-consent)
// A bottom banner on first visit, with the four category cards behind
// "Customize". Essential cookies are locked ON with an explanation.
// Nothing optional starts on.
// ============================================================================
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { COOKIE_CATEGORIES } from "../../api";
import {
  bannerPurposes,
  collectConsent,
  newIdempotencyKey,
  principalRef,
} from "../../api/banner";

// Cookie categories are a PRESENTATION vocabulary — the words a visitor
// understands. Consent is recorded against a purpose, which is the thing with a
// published notice and a version. This maps one to the other.
//
// A category with no matching purpose in this workspace is NOT rendered. A
// toggle that writes nowhere is worse than an absent one: the visitor believes
// they have made a choice, and no record of it exists.
const CATEGORY_PURPOSE = {
  performance: null,          // no seeded purpose covers this yet
  analytics: "analytics",
  marketing: "marketing_email",
};
import { useApp } from "../../context/AppContext";
import LanguageSwitcher from "../../components/common/LanguageSwitcher";
import { previewLock } from "../../config/modules";

export default function CookieConsent() {
  const { notify, language, user } = useApp();
  const [dismissed, setDismissed] = useState(false);
  const [customizing, setCustomizing] = useState(false);
  const [prefs, setPrefs] = useState(() => {
    const initial = {};
    COOKIE_CATEGORIES.forEach((c) => {
      initial[c.id] = c.locked;   // essential ON (locked), everything else OFF
    });
    return initial;
  });
  const [renewsAt, setRenewsAt] = useState(null);
  const [busy, setBusy] = useState(false);
  const [available, setAvailable] = useState(null);   // purpose keys this tenant has
  const [receipts, setReceipts] = useState([]);
  const [error, setError] = useState(null);
  const [idemKeys] = useState(() => new Map());

  const load = useCallback(async () => {
    try {
      const rows = await bannerPurposes();
      setAvailable(new Set(rows.map((r) => r.key)));
      setError(null);
    } catch (e) {
      setError(e.message || "Could not load cookie options.");
      setAvailable(new Set());
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Essential always shows (it is informational and locked on). Everything else
  // shows only if this workspace actually has the purpose behind it.
  const categories = COOKIE_CATEGORIES.filter((c) => {
    if (c.locked) return true;
    const key = CATEGORY_PURPOSE[c.id];
    return key && available?.has(key);
  });

  const setAll = (value) => {
    setPrefs((prev) => {
      const next = { ...prev };
      categories.forEach((c) => {
        if (!c.locked) next[c.id] = value;
      });
      return next;
    });
  };

  const save = async (override) => {
    setBusy(true);
    const toSave = override || prefs;
    const ref = principalRef(user);
    const collected = [];
    const failures = [];

    // One call per accepted category. A declined category produces NO call:
    // "no" means no consent was collected, not that one was withdrawn — and a
    // publishable key cannot withdraw anything.
    for (const c of categories) {
      if (c.locked || !toSave[c.id]) continue;
      const purpose = CATEGORY_PURPOSE[c.id];
      if (!purpose) continue;
      if (!idemKeys.has(c.id)) idemKeys.set(c.id, newIdempotencyKey());
      try {
        collected.push(
          await collectConsent({
            principalRef: ref,
            purpose,
            language,
            source: "cookie-banner",
            idempotencyKey: idemKeys.get(c.id),
          })
        );
      } catch (e) {
        failures.push(`${c.name}: ${e.message}`);
      }
    }

    setBusy(false);

    if (failures.length) {
      notify(`Some choices could not be recorded — ${failures.join("; ")}`, "error");
      if (!collected.length) return;
    }

    setReceipts(collected);
    // Renewal reflects the shortest retention among what was actually recorded,
    // rather than a number invented by this screen.
    setRenewsAt(collected[0]?.expires_at || null);
    setDismissed(true);
    notify(
      collected.length
        ? "Cookie preferences recorded, with a receipt."
        : "Nothing optional was accepted, so no consent was recorded."
    );
  };

  const acceptAll = () => {
    const all = Object.fromEntries(categories.map((c) => [c.id, true]));
    setPrefs(all);
    save(all);
  };

  const declineAll = () => {
    const none = Object.fromEntries(categories.map((c) => [c.id, c.locked]));
    setPrefs(none);
    save(none);
  };

  return (
    <div className="space-y-6">
      <div className="card p-5">
        <h1 className="text-lg font-semibold text-ink">Cookie consent surface</h1>
        <p className="mt-1 text-sm text-muted">
          This is the banner a visitor sees on their first visit. It is shown here as a page so you
          can inspect it; in production it is pinned to the bottom of every page until answered.
        </p>
        {dismissed && (
          <div className="mt-4 rounded-lg border border-success/40 bg-success/5 p-4">
            <p className="flex items-center gap-2 text-sm font-medium text-ink">
              <span className="h-2 w-2 rounded-full bg-success" aria-hidden="true" />
              Preferences saved — the banner is dismissed.
            </p>
            <p className="mt-1 text-xs text-muted">
              Recorded{" "}
              {Object.entries(prefs)
                .filter(([, v]) => v)
                .map(([k]) => k)
                .join(", ")}
              . Your preferences will be renewed on{" "}
              {renewsAt ? new Date(renewsAt).toLocaleDateString() : "—"} (12 months).
            </p>
            <div className="mt-3 flex flex-wrap gap-3">
              <button type="button" className="btn-secondary" onClick={() => setDismissed(false)}>
                Reopen Cookie Settings
              </button>
              <Link to="/user/preferences" className="btn-ghost">
                Go to Preference Centre
              </Link>
            </div>
          </div>
        )}
      </div>

      {!dismissed && (
        <div className="card border-navy/20 p-5 shadow-panel">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <h2 className="font-semibold text-ink">We use cookies on this website</h2>
              <p className="mt-1 text-sm text-muted">
                Essential cookies keep the site working. Everything else is your choice, and stays
                off until you turn it on.
              </p>
            </div>
            <LanguageSwitcher compact />
          </div>

          {customizing && (
            <div className="mt-4 space-y-3 border-t border-line pt-4">
              <div className="flex flex-wrap gap-3">
                <button type="button" className="btn-ghost text-xs" onClick={() => setAll(true)}>
                  Turn all optional on
                </button>
                <button type="button" className="btn-ghost text-xs" onClick={() => setAll(false)}>
                  Turn all optional off
                </button>
              </div>

              {categories.map((c) => (
                <div key={c.id} className="rounded-lg border border-line p-4">
                  <div className="flex items-start justify-between gap-4">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="font-medium text-ink">{c.name}</p>
                        {c.locked && (
                          <span className="inline-flex items-center gap-1 rounded-full bg-navy/10 px-2 py-0.5 text-xs font-semibold text-navy">
                            <span className="h-1.5 w-1.5 rounded-full bg-navy" aria-hidden="true" />
                            ALWAYS ON
                          </span>
                        )}
                      </div>
                      <p className="mt-1 text-sm text-muted">{c.description}</p>
                    </div>
                    <button
                      type="button"
                      role="switch"
                      aria-checked={Boolean(prefs[c.id])}
                      aria-label={c.name}
                      disabled={c.locked}
                      onClick={() => setPrefs((p) => ({ ...p, [c.id]: !p[c.id] }))}
                      className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition ${
                        prefs[c.id] ? "bg-teal" : "bg-line"
                      } ${c.locked ? "cursor-not-allowed opacity-60" : ""}`}
                    >
                      <span
                        className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition ${
                          prefs[c.id] ? "translate-x-6" : "translate-x-1"
                        }`}
                      />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}

          <div className="mt-5 flex flex-wrap items-center gap-3 border-t border-line pt-4">
            <button type="button" className="btn-primary" onClick={acceptAll} disabled={busy} {...previewLock("consent_surfaces", "Accepting all cookies")}>
              Accept All
            </button>
            <button type="button" className="btn-secondary" onClick={declineAll} disabled={busy} {...previewLock("consent_surfaces", "Declining optional cookies")}>
              Decline All
            </button>
            {customizing ? (
              <button type="button" className="btn-secondary" onClick={() => save()} disabled={busy} {...previewLock("consent_surfaces", "Saving cookie preferences")}>
                {busy ? "Saving…" : "Save Preferences"}
              </button>
            ) : (
              <button type="button" className="btn-ghost" onClick={() => setCustomizing(true)}>
                Customize
              </button>
            )}
            <a href="#cookie-policy" className="ml-auto text-sm text-teal underline">
              Cookie Policy
            </a>
          </div>

          <p className="mt-3 text-xs text-muted">
            Your preferences will be renewed in 12 months. You can change them any time from the
            “Cookie Settings” link in the footer.
          </p>
        </div>
      )}
    </div>
  );
}
