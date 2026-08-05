# Publishable keys — the security model

A publishable key (`pk_live_…`) ships inside a customer's web page so a consent
banner can record answers without a server round trip of their own. It is
**public**: it is in the JavaScript, anyone can read it, and it will end up in
someone's public repo. The design assumes all of that.

This is the same shape as a Stripe or Firebase publishable key, and the same
rule applies: **security comes from the key being incapable of harm, not from the
key being hard to obtain.**

---

## 1. The key cannot do damage

`consent:collect` and nothing else. It cannot withdraw a consent, cannot read
anyone's answers, cannot reach a DSAR, cannot list principals.

The single most important line in this model is the split of what used to be one
`consent:write` scope:

| | |
| --- | --- |
| `consent:collect` | record that someone agreed |
| `consent:withdraw` | destroy a record that someone agreed |

Those are not the same risk. A forged consent is a bad record that provenance can
expose after the fact. A forged *withdrawal* destroys genuine evidence and trips
the customer's downstream processing stops for a person who never asked — real
harm to a real individual, and unrecoverable. A credential in a browser bundle
must not be one keystroke away from that.

So withdrawal now requires either a verified session (the preference centre,
after identity verification) or a secret server-side key holding
`consent:withdraw`. A publishable key hitting a withdraw path gets a 403 naming
the scope it lacks, in exactly the shape the rest of the public API already uses.

The ceiling is enforced three times over, because one enforcement point is one
mistake away from being bypassed:

1. `PUBLISHABLE_SCOPES` in `core/permissions.py` — a constant, not a default.
2. `publishable_key_service.create_key` takes no capabilities parameter at all.
3. A `CHECK` constraint on `publishable_keys`. A console bug, a migration or a
   hand-written `UPDATE` still cannot put a withdraw capability in a bundle.

The banner router also has **no** withdraw endpoint. There is nothing to reach.

---

## 2. Provenance is where record trust comes from

Because the key is public, a record cannot be trusted on the strength of *who
submitted it*. It is trusted because the server observed and recorded the
circumstances, and put them in the tamper-evident audit chain.

Every banner-collected consent gets a `consent_provenance` row, all of it
server-derived:

| Field | Why |
| --- | --- |
| `origin` | which site the answer came from |
| `ip_hash` | keyed HMAC, never the raw IP — see below |
| `user_agent` | corroboration, and obvious bot signal |
| `notice_id` + `notice_version` | the exact wording in force at that moment |
| `collection_method` | `publishable_key` or `signed_token` |
| `strongly_bound` | whether the principal was *verified* or merely asserted |
| `server_receipt_id` + `received_at` | a handle for disputes, and a server clock |

None of these are in the request schema. A client that could set its own
provenance would be supplying its own alibi, so an attempt to send them is
ignored rather than honoured — and there is a test for that.

**Why the IP is hashed.** An IP is personal data under the DPDP Act, and a
consent-collection log is the wrong place to accumulate a second identifier for
everyone who ever saw a banner. The hash is keyed (HMAC) rather than a bare
SHA-256, because the IPv4 space is small enough to enumerate and an unkeyed
digest is reversible in practice. Correlation — "same client?" — still works,
which is what abuse investigation actually needs.

Provenance is **append-and-read**: no `UPDATE` or `DELETE` grant, and each row is
also hashed into the audit chain. Deleting the provenance row alone leaves a
consent whose history no longer matches the trail.

---

## 3. Origin pinning is defence-in-depth, not the boundary

Each key carries an explicit `allowed_origins` allowlist, and the collect
endpoint checks the `Origin` header against it (as a dependency, so it cannot be
forgotten on the next endpoint added to that router).

**`Origin` is set by browsers and trivially forged by anything that is not one.**
`curl` sends whatever you tell it to. This check raises the cost of casually
misusing a key lifted from a bundle; it stops nothing determined. It is listed
third here on purpose — if it were the security boundary, this design would not
be safe.

An empty allowlist means the key works from nowhere, and key creation refuses it:
handing someone a key that silently works from no origin is worse than refusing.

---

## 4. Rate limits: per key *and* per IP

Both, because either alone has an obvious hole. Per key only lets one abusive
client consume a customer's whole allowance and take their banner down for
everybody. Per IP only lets a distributed caller sail past.

Defaults are low for an unauthenticated public write path — 60/min per key,
10/min per IP — and configurable per key. Counted in `api_request_log`, the same
machinery the secret-key limiter uses, so the limit survives a restart and holds
across replicas rather than living in one process's memory.

---

## 5. The signed-token step-up

For sensitive-category consent, an asserted `principal_ref` is not good enough:
the page could claim to be anybody, and the record would say so but still be
weak.

The escape hatch: the integrator's own server — which has actually authenticated
the person — mints a short-lived signed token binding the `principal_ref`, and
the banner submits it alongside the consent.

```
token   = base64url(payload) "." base64url(HMAC-SHA256(secret, base64url(payload)))
payload = {"principal_ref": "...", "exp": <unix seconds>, "nonce": "..."}
secret  = per-tenant, from GET /v1/admin/consent-token-secret
TTL     = 300 seconds
```

Deliberately **not a JWT**. A JWT brings algorithm negotiation with it, and
`alg: none` against a permissive library is a well-worn forgery route. One
algorithm, one secret, no negotiation. The signature is verified with a
constant-time compare before the payload is trusted for anything.

| Request | Result |
| --- | --- |
| valid token | `collection_method: signed_token`, `strongly_bound: true`, and the token's `principal_ref` **overrides** whatever the body claimed |
| no token | falls back to publishable collect — `strongly_bound: false` |
| expired / forged / malformed | **403** |
| no token, but the key sets `require_signed_token` | **403** naming `consent_token` |

`strongly_bound: true` is only possible with a token: a `CHECK` constraint
refuses a provenance row claiming a verified binding under any other collection
method.

**Rotation is a known gap.** One secret per tenant with no versioning, so
rotating it invalidates every token in flight — tolerable at a 5-minute TTL, but
a `kid` in the payload and two live secrets would make it seamless. Marked
`TODO(rotation)` in `core/security.py` rather than half-built.

---

## 6. What this model does *not* claim

Stated plainly, because a security note that only lists strengths is not useful:

- **A determined caller can create junk consent records** for asserted
  `principal_ref` values within the rate limit. They will be attributable —
  origin, hashed IP, user agent, receipt id, all in the audit chain — and they
  will be `strongly_bound: false`, so they are distinguishable from verified ones.
  Preventing them entirely requires the signed-token step-up.
- **Origin pinning stops nothing that is not a browser.**
- **Rate limiting is a fixed window**, so a caller can get 2N across a boundary.
- **The audit chain cannot detect truncation of its newest entries.** External
  anchoring (Phase 10) is the answer, and it is not built.

If a customer's purposes cannot tolerate the first point, they should set
`require_signed_token` on the key. That is what it is for.
