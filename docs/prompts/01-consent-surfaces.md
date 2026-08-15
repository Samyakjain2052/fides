# Build brief — `consent_surfaces` (public consent & cookie banners)

> Paste this whole file as the opening prompt. Fill the `[DECIDE]` block first.

**Size: hours, not days.** The backend is already built, tested and pushed. This
brief is almost entirely frontend.

---

## 1. What already exists

Migration `0004_publishable_keys` shipped a complete browser-safe collection API.
Read [`docs/PUBLISHABLE_KEY_SECURITY.md`](../PUBLISHABLE_KEY_SECURITY.md) before
writing code — it explains *why* the endpoints are shaped the way they are.

```
GET  /public/v1/banner/purposes   what a banner may offer + the notice wording
POST /public/v1/banner/consent    collect only. There is no withdraw path here.
```

Auth: `X-Publishable-Key: pk_live_…` in its own header (never `Authorization`).
Issued from `POST /v1/admin/publishable-keys` with an `allowed_origins` list.

Already handled server-side, so **do not reimplement any of it**:

- collect-only capability, enforced three times over (constant, service, CHECK)
- origin pinning
- per-key and per-IP rate limits
- `Idempotency-Key` replay
- provenance stamping (origin, hashed IP, user agent, notice version, receipt id)
  into the audit chain
- the optional signed-token step-up
- mandatory purposes are excluded from `/banner/purposes` and refused on collect

32 tests cover it (`backend/tests/test_publishable_keys.py`).

## 2. The gap

`frontend/src/pages/user/ConsentBanner.jsx` and `CookieConsent.jsx` still call
the in-memory mock in `src/api/index.js` (`saveConsentChoices`,
`saveCookiePreferences`, `submitGuardianConsent`). Their controls are disabled by
`previewLock("consent_surfaces", …)`.

## 3. `[DECIDE]` — answer before writing code

- `[DECIDE]` **How does the banner get its publishable key?**
  These are unauthenticated pages. Options:
  **(a)** a build-time env var (`VITE_PUBLISHABLE_KEY`) — simplest, right for the
  demo, means one key for the deployed instance;
  **(b)** a `?pk=` query parameter — lets one deployment demo several tenants;
  **(c)** read it from `window.DataShieldConfig` injected by the host page — what
  a real customer embed would do.
  **Recommendation: (a) with (c) as an override**, so the demo works out of the
  box and the embed story is still demonstrable.

- `[DECIDE]` **What is `principal_ref` for an anonymous visitor?**
  A first-time visitor has no account. Options: a random id in `localStorage`
  (survives reloads, is the honest "this browser" identifier), or the signed-in
  user's id when there is a session.
  **Recommendation: both** — use the session identity when signed in, otherwise a
  `localStorage` visitor id, and record which in `source`.

## 4. What to build

### 4.1 `src/api/banner.js` (new)

The only file that talks to the banner API. **Does not use `apiFetch`** from
`api/auth.js` — that sends a Bearer token and `credentials: include`, and these
pages have neither. Plain `fetch` with the publishable-key header.

```js
export async function bannerPurposes()            // GET  /public/v1/banner/purposes
export async function collectConsent({ principalRef, purpose, language, source,
                                       consentToken, idempotencyKey })
```

- Send an `Idempotency-Key` per logical submission (a UUID generated once when
  the banner mounts, not per click) so a double-click or a flaky network cannot
  record two consents.
- Surface the server's `detail` on failure. The endpoint returns real reasons
  (409 for a mandatory purpose, 403 for a bad origin) and swallowing them turns a
  fixable integration mistake into "the banner doesn't work".

### 4.2 `ConsentBanner.jsx`

- Render one card per purpose returned by `/banner/purposes`, using the **notice
  wording from the server** — not hardcoded copy. This is the text the person is
  agreeing to and the version is recorded against it.
- **Every optional purpose starts OFF.** No pre-checked toggles, ever. There must
  be no code path that defaults one on, and a test must assert it.
- "Accept all optional" / "Decline all optional" may stay as local state helpers;
  only "Save my choices" writes.
- Saving posts **one call per granted purpose**. Declining a purpose means *not
  collecting consent for it* — never a withdraw call, because a publishable key
  cannot withdraw and must not appear to.
- Show the returned `server_receipt_id` on the confirmation. It is the person's
  handle if they ever dispute the record, and showing it is the difference
  between a claim and a receipt.
- **The guardian flow (under-18) stays `preview`.** DPDP §9 needs verifiable
  parental consent, and the publishable-key path cannot verify a guardian. Leave
  its controls locked and say why on screen.

### 4.3 `CookieConsent.jsx`

- Same rules. Essential cookies stay locked ON with "Required by law" visible —
  never hidden.
- Map the four cookie categories onto real purposes. If a tenant has no purpose
  for a category, **do not render that category** rather than showing a toggle
  that writes nowhere.

### 4.4 Honest failure

If the key is missing, revoked, or the origin is not allowed, the banner must say
so plainly — not silently render a dead form. A consent banner that appears to
work and records nothing is the worst possible outcome for a compliance product.

## 5. Non-goals

- Do not add a withdraw path to the banner. Withdrawal needs a verified session
  (the preference centre) or a secret key with `consent:withdraw`.
- Do not build a script-tag embed bundle. That is a packaging exercise; this is
  about the two screens in the app.
- Do not touch the publishable-key backend. It is done.

## 6. Tests

Add to `frontend/` (or extend the backend endpoint tests where it is a server
behaviour):

- no optional purpose renders checked on first load
- declining produces **zero** write calls, not a withdraw
- a granted purpose posts exactly once, with the `Idempotency-Key`
- a second click within the same mount replays rather than double-recording
- a 403 (bad origin) and a 409 (mandatory purpose) both surface the server's
  message on screen
- mandatory purposes never appear as toggles

## 7. Definition of done

- [ ] Both screens read and write `/public/v1/banner/*`
- [ ] `consent_surfaces` flipped to `"live"` in `src/config/modules.js`
- [ ] The guardian sub-flow left `preview` with a caveat in `MODULE_CAVEATS`
- [ ] `npm run build` passes (the preview-lock check gates it)
- [ ] `./scripts/acceptance.sh http://localhost:8090` still passes
- [ ] A real browser run: load the banner, accept one purpose, decline another,
      confirm exactly one consent row and one provenance row exist, and that the
      provenance appears in `/admin/audit`

## 8. House rules

Reuse existing patterns; do not build parallel ones. Timezone-aware timestamps.
Every state change reaches the audit chain (the server already does this).
Nothing on screen may imply a capability that does not exist. Full list:
[README.md](README.md).
