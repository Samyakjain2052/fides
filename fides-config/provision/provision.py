#!/usr/bin/env python3
"""
Provision a running Fides server from the version-controlled files in
/fides-config.

Runs once, on `docker compose up`, inside the `fides-provisioner` service. That
service uses the *same* ethyca/fides image as the server, so the `fides` CLI is
available here and we use it for the parts it covers.

    +-------------------------------+--------------------------------------+
    | resource                      | loaded by                            |
    +-------------------------------+--------------------------------------+
    | Systems, Datasets (fideslang) | `fides push` CLI                     |
    | Local storage destination     | PUT   /api/v1/storage/default        |
    | ConnectionConfigs             | PATCH /api/v1/connection             |
    | Connection secrets            | PUT   /api/v1/connection/{k}/secret  |
    | Dataset <-> connection link   | PATCH /api/v1/connection/{k}/        |
    |                               |         datasetconfig                |
    | Connection <-> system link    | PUT   /api/v1/connection/{k}/        |
    |                               |         system-links                 |
    | Custom SaaS connector template| POST  /api/v1/connector-templates/   |
    |  (Zoho CRM — no built-in      |         register                     |
    |  Fides connector exists)      |                                       |
    | SaaS connection + its dataset | POST  /api/v1/connection/instantiate/|
    |  (created together)           |         {connector_type}             |
    | DSR policies / rules / targets| PATCH /api/v1/dsr/policy[/...]       |
    +-------------------------------+--------------------------------------+

The CLI has no commands for the API rows above, which is why they go through
the API. Everything is an upsert, so this script is idempotent: run it as many
times as you like.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
from typing import Any
from zipfile import ZipFile

import requests
import yaml

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
FIDES_URL = os.environ.get("FIDES_URL", "http://fides:8080").rstrip("/")
API = f"{FIDES_URL}/api/v1"
USERNAME = os.environ["FIDES_ROOT_USERNAME"]
PASSWORD = os.environ["FIDES_ROOT_PASSWORD"]

CONFIG_DIR = os.environ.get("FIDES_CONFIG_DIR", "/fides-config")
RESOURCES_DIR = f"{CONFIG_DIR}/resources"
CONNECTIONS_FILE = f"{CONFIG_DIR}/connections/connections.yml"
POLICIES_FILE = f"{CONFIG_DIR}/policies/dsr_policies.yml"
# Each subdirectory is a custom SaaS connector template: a config.yml +
# dataset.yml pair, registered with Fides before any `saas_connection` entry
# in connections.yml can reference it. See register_saas_connector_templates().
SAAS_DIR = f"{CONFIG_DIR}/saas"

# Where access-request packages get written inside the Fides container.
# Bind-mounted to ./fides_uploads on the host by docker-compose.yml.
STORAGE_KEY = "demo_local_storage"

TIMEOUT = 60


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[provision] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"[provision] FATAL: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def wait_for_fides(max_wait: int = 300) -> None:
    """Block until the Fides server reports healthy."""
    log(f"waiting for Fides at {FIDES_URL} ...")
    deadline = time.time() + max_wait
    last = ""
    while time.time() < deadline:
        try:
            r = requests.get(f"{FIDES_URL}/health", timeout=5)
            if r.status_code == 200:
                log(f"Fides is up: {r.json().get('version', 'unknown version')}")
                return
            last = f"HTTP {r.status_code}"
        except requests.RequestException as exc:  # not up yet
            last = type(exc).__name__
        time.sleep(3)
    die(f"Fides did not become healthy within {max_wait}s (last: {last})")


def login() -> str:
    """POST /api/v1/login -> bearer token.

    Fides' root user is configured via security.root_username/root_password in
    fides.toml. The token it returns carries the root role, i.e. every scope.
    """
    r = requests.post(
        f"{API}/login",
        json={"username": USERNAME, "password": PASSWORD},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        die(f"login failed: HTTP {r.status_code} {r.text}")
    token = r.json()["token_data"]["access_token"]
    log(f"authenticated as '{USERNAME}'")
    return token


class Fides:
    """Thin authenticated wrapper around the Fides API."""

    def __init__(self, token: str) -> None:
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}"})

    def call(self, method: str, path: str, payload: Any = None, ok=(200,)) -> Any:
        r = self.s.request(method, f"{API}{path}", json=payload, timeout=TIMEOUT)
        if r.status_code not in ok:
            die(f"{method} {path} -> HTTP {r.status_code}\n{r.text}")
        try:
            return r.json()
        except ValueError:
            return None

    def exists(self, path: str) -> bool:
        """GET path and report whether it resolves (200) or not (404).

        Unlike call(), this needs the actual status code rather than a
        JSON-or-None body — a 404 still comes back with a JSON error body, so
        `call(..., ok=(200, 404))` can't distinguish "found" from "not found".
        """
        r = self.s.request("GET", f"{API}{path}", timeout=TIMEOUT)
        if r.status_code in (200, 404):
            return r.status_code == 200
        die(f"GET {path} -> HTTP {r.status_code}\n{r.text}")

    @staticmethod
    def check_bulk(label: str, resp: Any) -> None:
        """Fides bulk endpoints return 200 even when individual items fail.

        The failures land in a `failed` list in the body, so a naive status-code
        check would let a broken config through. Treat any failure as fatal.
        """
        if not isinstance(resp, dict):
            return
        failed = resp.get("failed") or []
        if failed:
            die(f"{label}: {len(failed)} item(s) failed:\n{yaml.safe_dump(failed)}")
        log(f"{label}: {len(resp.get('succeeded') or [])} succeeded")


def expandvars(text: str) -> str:
    """Expand $VAR / ${VAR} against the environment, like Docker/bash.

    Mirrors what Fides itself does when loading its bundled sample connections
    (see src/fides/api/db/samples.py), so secrets can stay out of the YAML.
    """
    return os.path.expandvars(text)


def load_yaml(path: str, expand: bool = False) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    if expand:
        raw = expandvars(raw)
    return yaml.safe_load(raw) or {}


# --------------------------------------------------------------------------
# Step 1 — Systems + Datasets, via the `fides` CLI
# --------------------------------------------------------------------------
def run_cli(args: list[str], label: str) -> None:
    """Run a `fides` CLI command, streaming its output, and die on failure."""
    proc = subprocess.run(args, capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        die(f"{label} exited {proc.returncode}")


def push_ctl_resources() -> None:
    """`fides push` the fideslang resources in /fides-config/resources.

    Server host/port come from fides.toml (FIDES__CONFIG_PATH), which this
    container mounts read-only alongside the server.

    IMPORTANT (this one is easy to get wrong): the CLI does NOT authenticate
    from `[user] username/password` in fides.toml. It reads a bearer token out
    of a credentials file at ~/.fides_credentials, which only
    `fides user login` writes. Skip the login and `fides push` gets as far as
    the taxonomy and then fails every dataset with
    `{'detail': 'Not Authorized for this action'}`.
    The token is also loaded at import time, so the login has to happen in a
    separate process — hence two subprocess calls rather than one session.
    Reference: fides/core/utils.py::get_auth_header
    """
    log("`fides user login` (writes ~/.fides_credentials for the CLI) ...")
    run_cli(
        ["fides", "user", "login", "--username", USERNAME, "--password", PASSWORD],
        "`fides user login`",
    )

    log(f"`fides push {RESOURCES_DIR}` (systems + datasets) ...")
    run_cli(["fides", "push", RESOURCES_DIR], "`fides push`")
    log("systems + datasets pushed")


# --------------------------------------------------------------------------
# Step 2 — Local storage destination for access packages
# --------------------------------------------------------------------------
def configure_storage(f: Fides) -> None:
    """Access rules need somewhere to put the assembled data package.

    `local` writes JSON to the Fides container's filesystem and is explicitly
    documented as test-only. A real deployment would use s3 or gcs here.
    """
    log("configuring default 'local' storage destination ...")

    # The default-storage endpoint is what access rules fall back to, and what
    # the Admin UI shows under Privacy Requests > Configuration.
    f.call(
        "PUT",
        "/storage/default",
        {"type": "local", "format": "json", "details": {"naming": "request_id"}},
    )

    # Also register it as a *named* destination so dsr_policies.yml can point at
    # it explicitly via storage_destination_key.
    resp = f.call(
        "PATCH",
        "/storage/config",
        [
            {
                "key": STORAGE_KEY,
                "name": "Demo Local Storage",
                "type": "local",
                "format": "json",
                "details": {"naming": "request_id"},
            }
        ],
    )
    Fides.check_bulk("storage config", resp)


# --------------------------------------------------------------------------
# Step 3 — ConnectionConfigs, secrets, dataset + system links
# --------------------------------------------------------------------------
def configure_connections(f: Fides) -> None:
    doc = load_yaml(CONNECTIONS_FILE, expand=True)
    connections = doc.get("connection", [])
    if not connections:
        die(f"no `connection:` entries found in {CONNECTIONS_FILE}")

    # Fail loudly on an unexpanded $VAR rather than shipping the literal string
    # to Fides as a hostname and getting a confusing connection error later.
    for conn in connections:
        for name, value in (conn.get("secrets") or {}).items():
            if isinstance(value, str) and value.startswith("$"):
                die(
                    f"connection '{conn['key']}' secret '{name}' is unresolved "
                    f"({value}). Is it set in .env?"
                )

    # 3a. Upsert the connections themselves (without secrets — those go
    #     through the dedicated endpoint so Fides validates them per type).
    payload = [
        {
            "key": c["key"],
            "name": c.get("name", c["key"]),
            "description": c.get("description"),
            "connection_type": c["connection_type"],
            "access": c.get("access", "write"),
            "disabled": c.get("disabled", False),
        }
        for c in connections
    ]
    log(f"upserting {len(payload)} connection(s) ...")
    Fides.check_bulk("connections", f.call("PATCH", "/connection", payload))

    for c in connections:
        key = c["key"]

        # 3b. Secrets. `verify=true` makes Fides actually open the connection,
        #     so a bad password fails here rather than mid-DSAR.
        #
        #     Retried: a database can pass its Docker healthcheck a moment
        #     before it accepts outside connections (Mongo in particular runs
        #     its init scripts against localhost only, then restarts), and a
        #     wrong-password failure looks identical to a not-ready-yet one.
        log(f"  [{key}] writing secrets and verifying connectivity ...")
        attempts, result, status = 10, None, None
        for attempt in range(1, attempts + 1):
            result = f.call("PUT", f"/connection/{key}/secret?verify=true", c["secrets"])
            status = (result or {}).get("test_status")
            if status == "succeeded":
                break
            if attempt < attempts:
                log(f"  [{key}] not ready (attempt {attempt}/{attempts}); retrying in 5s")
                time.sleep(5)
        if status != "succeeded":
            die(
                f"connection '{key}' failed its connectivity test after "
                f"{attempts} attempts (test_status={status!r}, failure_reason="
                f"{(result or {}).get('failure_reason')!r})"
            )
        log(f"  [{key}] connection test succeeded")

        # 3c. Link the pushed Dataset to this connection. A Dataset on its own
        #     is just documentation in the data map; the DatasetConfig is what
        #     makes it executable against a real datastore.
        dataset_key = c.get("dataset")
        if dataset_key:
            log(f"  [{key}] linking dataset '{dataset_key}' ...")
            Fides.check_bulk(
                f"  [{key}] datasetconfig",
                f.call(
                    "PATCH",
                    f"/connection/{key}/datasetconfig",
                    [
                        {
                            "fides_key": dataset_key,
                            "ctl_dataset_fides_key": dataset_key,
                        }
                    ],
                ),
            )

        # 3d. Attach the connection to its System so the Admin UI data map
        #     shows the integration under the right system. PATCH
        #     /system/{key}/connection is deprecated and (as of 2.86) doesn't
        #     populate the newer system<->connection join table the Admin UI
        #     actually reads from — PUT /connection/{key}/system-links does.
        system_key = c.get("system_key")
        if system_key:
            log(f"  [{key}] attaching to system '{system_key}' ...")
            f.call(
                "PUT",
                f"/connection/{key}/system-links",
                {"links": [{"system_fides_key": system_key}]},
            )


# --------------------------------------------------------------------------
# Step 3.5 — Custom SaaS connectors (Zoho CRM)
#
# A SaaS API (reached over HTTPS, no native driver) can't use the postgres/
# mongodb path above, and Fides ships no built-in Zoho CRM connector — so this
# is a two-part process instead of the DB connectors' upsert-then-link:
#   1. register_saas_connector_templates(): teach Fides the `zoho_crm`
#      connector type at all, by uploading fides-config/saas/zoho_crm/ as a
#      connector template zip.
#   2. configure_saas_connections(): create the ConnectionConfig + its
#      auto-generated Dataset together from that template, in one call.
# --------------------------------------------------------------------------
def register_saas_connector_templates(f: Fides) -> None:
    """Upload every fides-config/saas/<connector>/ directory as a connector
    template zip via POST /connector-templates/register.

    Idempotent: re-registering the same connector type just overwrites its
    stored template (CustomConnectorTemplateLoader.save_template upserts).
    """
    if not os.path.isdir(SAAS_DIR):
        return

    for connector_type in sorted(os.listdir(SAAS_DIR)):
        connector_dir = os.path.join(SAAS_DIR, connector_type)
        config_path = os.path.join(connector_dir, "config.yml")
        dataset_path = os.path.join(connector_dir, "dataset.yml")
        if not (os.path.isfile(config_path) and os.path.isfile(dataset_path)):
            continue

        log(f"registering custom SaaS connector template '{connector_type}' ...")
        buf = io.BytesIO()
        with ZipFile(buf, "w") as zf:
            zf.write(config_path, "config.yml")
            zf.write(dataset_path, "dataset.yml")
        buf.seek(0)

        r = requests.post(
            f"{API}/connector-templates/register",
            headers=f.s.headers,
            files={"file": (f"{connector_type}.zip", buf, "application/zip")},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            die(f"registering connector template '{connector_type}' -> HTTP {r.status_code}\n{r.text}")
        log(f"  [{connector_type}] template registered")


def refresh_zoho_access_token() -> str:
    """Exchange the long-lived Zoho refresh token for a short-lived access
    token, using Zoho's own OAuth endpoint (this call goes to Zoho, not Fides).

    Zoho access tokens expire after an hour; the refresh token does not
    expire, so this runs once per `docker compose up` rather than needing
    Fides' own OAuth2 token-refresh machinery (which — see config.yml's header
    comment — hardcodes a header format Zoho doesn't accept anyway).
    """
    accounts_domain = os.environ.get("ZOHO_CRM_ACCOUNTS_DOMAIN", "accounts.zoho.com")
    client_id = os.environ.get("ZOHO_CRM_CLIENT_ID")
    client_secret = os.environ.get("ZOHO_CRM_CLIENT_SECRET")
    refresh_token = os.environ.get("ZOHO_CRM_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        die(
            "ZOHO_CRM_CLIENT_ID, ZOHO_CRM_CLIENT_SECRET and ZOHO_CRM_REFRESH_TOKEN "
            "must all be set in .env to provision the Zoho CRM connection."
        )

    log(f"refreshing Zoho CRM access token via {accounts_domain} ...")
    r = requests.post(
        f"https://{accounts_domain}/oauth/v2/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
        timeout=TIMEOUT,
    )
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    access_token = body.get("access_token")
    if r.status_code != 200 or not access_token:
        die(f"Zoho CRM token refresh failed: HTTP {r.status_code} {r.text}")
    log("Zoho CRM access token refreshed")
    return access_token


def configure_saas_connections(f: Fides) -> None:
    """Create (or refresh the secrets of) each `saas_connection:` entry.

    Unlike the DB connectors in configure_connections(), a SaaS connector's
    ConnectionConfig and Dataset are created together in one call — see
    POST /connection/instantiate/{connector_type} — so there's no separate
    datasetconfig-linking step.
    """
    doc = load_yaml(CONNECTIONS_FILE, expand=True)
    saas_connections = doc.get("saas_connection", [])
    if not saas_connections:
        return

    for c in saas_connections:
        key = c["key"]
        secrets = dict(c["secrets"])

        # access_token is a deliberate placeholder in connections.yml (never
        # set in .env) — it's always overwritten here with a freshly
        # refreshed, short-lived token, so it must be excluded from the
        # unresolved-$VAR check below.
        if "access_token" in secrets:
            secrets["access_token"] = refresh_zoho_access_token()

        for name, value in secrets.items():
            if isinstance(value, str) and value.startswith("$"):
                die(
                    f"saas_connection '{key}' secret '{name}' is unresolved "
                    f"({value}). Is it set in .env?"
                )

        existing = f.exists(f"/connection/{key}")
        if not existing:
            log(f"instantiating SaaS connection '{key}' from template '{c['connector_type']}' ...")
            f.call(
                "POST",
                f"/connection/instantiate/{c['connector_type']}",
                {
                    "key": key,
                    "name": c.get("name", key),
                    "description": c.get("description"),
                    "secrets": secrets,
                    "instance_key": c["instance_key"],
                },
                ok=(200, 201),
            )
        else:
            log(f"  [{key}] connection already exists; refreshing secrets ...")
            result = f.call("PUT", f"/connection/{key}/secret?verify=true", secrets)
            if (result or {}).get("test_status") != "succeeded":
                die(
                    f"connection '{key}' failed its connectivity test "
                    f"(test_status={(result or {}).get('test_status')!r}, "
                    f"failure_reason={(result or {}).get('failure_reason')!r})"
                )

        # See the note in configure_connections() above: PUT .../system-links
        # is the endpoint that actually populates what the Admin UI reads,
        # unlike the deprecated PATCH /system/{key}/connection.
        system_key = c.get("system_key")
        if system_key:
            log(f"  [{key}] attaching to system '{system_key}' ...")
            f.call(
                "PUT",
                f"/connection/{key}/system-links",
                {"links": [{"system_fides_key": system_key}]},
            )
        log(f"  [{key}] SaaS connection configured")


# --------------------------------------------------------------------------
# Step 4 — DSR policies, rules, rule targets
# --------------------------------------------------------------------------
def configure_policies(f: Fides) -> None:
    doc = load_yaml(POLICIES_FILE)
    policies = doc.get("policy", [])
    if not policies:
        die(f"no `policy:` entries found in {POLICIES_FILE}")

    log(f"upserting {len(policies)} DSR policy/policies ...")
    Fides.check_bulk(
        "dsr policies",
        f.call(
            "PATCH",
            "/dsr/policy",
            [
                {
                    "key": p["key"],
                    "name": p["name"],
                    **({"drp_action": p["drp_action"]} if p.get("drp_action") else {}),
                }
                for p in policies
            ],
        ),
    )

    for p in policies:
        pkey = p["key"]

        rules = []
        for r in p.get("rules", []):
            rule: dict[str, Any] = {
                "key": r["key"],
                "name": r["name"],
                "action_type": r["action_type"],
            }
            if r.get("storage_destination_key"):
                rule["storage_destination_key"] = r["storage_destination_key"]
            if r.get("masking_strategy"):
                rule["masking_strategy"] = r["masking_strategy"]
            rules.append(rule)

        log(f"  [{pkey}] upserting {len(rules)} rule(s) ...")
        Fides.check_bulk(
            f"  [{pkey}] rules", f.call("PATCH", f"/dsr/policy/{pkey}/rule", rules)
        )

        for r in p.get("rules", []):
            targets = [
                {
                    "key": t["key"],
                    "name": t.get("name", t["key"]),
                    "data_category": t["data_category"],
                }
                for t in r.get("targets", [])
            ]
            if not targets:
                continue
            log(f"  [{pkey}/{r['key']}] upserting {len(targets)} target(s) ...")
            Fides.check_bulk(
                f"  [{pkey}/{r['key']}] targets",
                f.call(
                    "PATCH",
                    f"/dsr/policy/{pkey}/rule/{r['key']}/target",
                    targets,
                ),
            )


# --------------------------------------------------------------------------
# Step 5 — Verify the graph is actually traversable
# --------------------------------------------------------------------------
def verify(f: Fides) -> None:
    """Catch the classic misconfigurations before the user runs their first DSAR.

    An unreachable collection is the #1 Fides gotcha: the dataset loads fine,
    the connection tests fine, and then the privacy request quietly returns
    nothing for that collection because no `identity` or `references` path
    leads to it.
    """
    log("verifying dataset reachability ...")
    doc = load_yaml(CONNECTIONS_FILE, expand=True)
    problems = []
    for c in doc.get("connection", []):
        dataset_key = c.get("dataset")
        if not dataset_key:
            continue
        result = f.call(
            "GET", f"/connection/{c['key']}/dataset/{dataset_key}/reachability"
        )
        if result and result.get("reachable"):
            log(f"  [{dataset_key}] reachable")
        else:
            problems.append(f"  [{dataset_key}] NOT reachable: {result}")

    for c in doc.get("saas_connection", []):
        dataset_key = c.get("instance_key")
        if not dataset_key:
            continue
        result = f.call(
            "GET", f"/connection/{c['key']}/dataset/{dataset_key}/reachability"
        )
        if result and result.get("reachable"):
            log(f"  [{dataset_key}] reachable")
        else:
            problems.append(f"  [{dataset_key}] NOT reachable: {result}")

    if problems:
        die("dataset reachability check failed:\n" + "\n".join(problems))

    # Confirm both DSR policies exist and carry the rules we expect.
    for pkey, action in (("demo_access_policy", "access"),
                         ("demo_erasure_policy", "erasure")):
        policy = f.call("GET", f"/dsr/policy/{pkey}")
        actions = [r["action_type"] for r in (policy.get("rules") or [])]
        if action not in actions:
            die(f"policy '{pkey}' has no '{action}' rule (rules: {actions})")
        log(f"  [{pkey}] has an '{action}' rule")


# --------------------------------------------------------------------------
def refresh_expiring_secrets(f: Fides) -> int:
    """Re-push only the short-lived secrets. Returns how many were refreshed.

    A narrow path through what `configure_saas_connections` does, for the one
    problem that recurs: Zoho access tokens expire after an hour, so DSAR
    acceptance started failing roughly hourly and the fix was to re-run the whole
    provisioner by hand. That is a standing operational chore hiding a defect.

    Deliberately narrow. It does not instantiate connections, register templates,
    touch datasets, systems or policies — a token refresh must not be able to
    reshape the configuration, and running full provisioning on a timer eventually
    reverts something somebody changed in the UI on purpose.

    Only connections that actually hold an `access_token` secret are touched. A
    connector whose credential does not expire is left alone.
    """
    doc = load_yaml(CONNECTIONS_FILE, expand=True)
    refreshed = 0
    for c in doc.get("saas_connection", []) or []:
        key = c["key"]
        secrets = dict(c.get("secrets") or {})
        if "access_token" not in secrets:
            continue
        if not f.exists(f"/connection/{key}"):
            log(f"  [{key}] not provisioned yet; skipping (run full provisioning first)")
            continue

        secrets["access_token"] = refresh_zoho_access_token()
        # verify=true so a refresh that produced an unusable token fails here,
        # loudly, rather than at 3am inside somebody's rights request.
        result = f.call("PUT", f"/connection/{key}/secret?verify=true", secrets)
        status = (result or {}).get("test_status")
        if status != "succeeded":
            die(
                f"connection '{key}' failed its connectivity test after a token "
                f"refresh (test_status={status!r}, "
                f"failure_reason={(result or {}).get('failure_reason')!r})"
            )
        log(f"  [{key}] access token refreshed and verified")
        refreshed += 1
    return refreshed


def refresh_loop() -> None:
    """Refresh on a timer, until killed.

    Zoho's tokens last an hour, so the default is 45 minutes — comfortably inside
    the window without being wasteful. The refresh token itself does not expire, so
    there is nothing here that needs a human.

    A failure sleeps and retries rather than exiting: a transient Zoho outage
    should not leave the connector permanently stale, and a container that exits
    on the first network blip would be restarted into the same blip.
    """
    every = int(os.environ.get("ZOHO_REFRESH_INTERVAL_SECONDS", 45 * 60))
    log(f"secret refresher started; every {every}s")
    while True:
        try:
            f = Fides(login())
            n = refresh_expiring_secrets(f)
            log(f"refreshed {n} connection secret(s)")
        except SystemExit:
            # die() raises SystemExit. Log it and keep the loop alive — see above.
            log("refresh failed; will try again at the next interval")
        except Exception as exc:  # noqa: BLE001
            log(f"refresh failed ({type(exc).__name__}: {exc}); retrying next interval")
        time.sleep(every)


def main() -> None:
    log("=" * 68)
    log("Provisioning Fides from /fides-config")
    log("=" * 68)

    wait_for_fides()
    push_ctl_resources()

    f = Fides(login())
    configure_storage(f)
    register_saas_connector_templates(f)
    configure_connections(f)
    configure_saas_connections(f)
    configure_policies(f)
    verify(f)

    log("=" * 68)
    log("Fides is configured and ready.")
    log("  Admin UI      : http://localhost:%s" % os.environ.get("FIDES_PORT", "8080"))
    log("  Gateway docs  : http://localhost:%s/docs" % os.environ.get("GATEWAY_PORT", "8000"))
    log("  Access policy : demo_access_policy")
    log("  Erasure policy: demo_erasure_policy")
    log("=" * 68)


if __name__ == "__main__":
    # Two modes from one image. `--refresh-secrets` is the long-running token
    # refresher; no argument is the one-shot full provisioning run.
    if "--refresh-secrets" in sys.argv:
        refresh_loop()
    elif "--refresh-secrets-once" in sys.argv:
        sys.exit(0 if refresh_expiring_secrets(Fides(login())) >= 0 else 1)
    else:
        main()
