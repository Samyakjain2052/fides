"""
Configuration — 12-factor, and it fails fast.

Every setting comes from the environment. There are no production defaults for
anything secret: if a secret is missing the process refuses to start, rather than
booting with a guessable fallback that nobody notices until it is on the internet.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="DS_",
        extra="ignore",
    )

    # ---------------------------------------------------------------- app --
    env: Literal["dev", "test", "staging", "prod"] = "dev"
    debug: bool = False
    api_prefix: str = "/v1"
    project_name: str = "DataShield API"

    # CORS: the browser origins allowed to call us. Never "*" once auth cookies
    # are in play — a wildcard origin plus credentials is a CSRF invitation.
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # -------------------------------------------------------------- database --
    # The APPLICATION connects with this URL, as a role that RLS applies to.
    # Migrations use `database_owner_url` instead (see db/session.py).
    database_url: PostgresDsn
    database_owner_url: PostgresDsn | None = None
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_echo: bool = False

    # TLS to the database.
    #
    # NOTE: you cannot do this with `?sslmode=require` in the URL. `sslmode` is a
    # libpq parameter and asyncpg does not accept it — it raises
    # "connect() got an unexpected keyword argument 'sslmode'". asyncpg wants an
    # ssl.SSLContext passed through connect_args, which is what
    # app/db/session.py::build_ssl_context does with these settings.
    #
    #   disable      local Docker only
    #   require      encrypted, server identity NOT verified
    #   verify-full  encrypted AND hostname/CA verified — use this on Azure,
    #                because `require` alone does not stop a MITM
    db_ssl_mode: Literal["disable", "require", "verify-full"] = "disable"
    # Optional CA bundle. Azure's chain (DigiCert Global Root G2 / Microsoft RSA
    # Root CA 2017) is usually already in the system trust store, so leaving this
    # unset normally works. Set it to pin explicitly.
    db_ssl_root_cert: str | None = None

    # ------------------------------------------------------------ security --
    # 32+ random bytes. Rotating this invalidates every access token, which is
    # the intended emergency lever.
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 14

    # Separate key, separate blast radius: this one signs the audit hash chain.
    # An attacker with database write access still cannot forge history without
    # it, which is the whole point of using HMAC rather than a bare hash.
    audit_hmac_key: str

    # Argon2id cost. Defaults are OWASP-ish for a web request budget; raise on
    # beefier hardware. Tests lower them so the suite doesn't take minutes.
    argon2_time_cost: int = 3
    argon2_memory_cost: int = 65536  # KiB
    argon2_parallelism: int = 4

    # The path prefix the BROWSER sees, when this API is served under one.
    #
    # The refresh cookie is scoped to `<prefix>/v1/auth` so it is not attached to
    # every request. That scoping breaks the moment a reverse proxy mounts the
    # API somewhere other than the root: nginx serves it at /api, the browser
    # asks for /api/v1/auth/refresh, and a cookie scoped to /v1/auth is simply
    # not sent. Sign-in succeeds, the next reload signs you out, and nothing is
    # logged anywhere because the request genuinely arrived without a cookie.
    #
    # Empty when the API is served at the root (running it directly).
    external_path_prefix: str = ""

    # Where a person who receives an email should click.
    #
    # Links we email are the one thing that MUST NOT be derived from the incoming
    # request. Two reasons, and the second is the serious one:
    #
    #   1. The request may not arrive on a public address at all. In Container
    #      Apps the backend runs on internal ingress, and nginx must send
    #      `Host: $proxy_host` or the platform will not route to it — so
    #      `request.base_url` is the backend's *internal* FQDN. An invitation
    #      built from it pointed at
    #      cms-backend.internal.<env>.azurecontainerapps.io, which resolves for
    #      nobody, and the recipient got Azure's "this Container App is stopped
    #      or does not exist" page.
    #
    #   2. `request.base_url` ultimately comes from the Host header, which the
    #      client chooses. Minting a link from it means somebody can make this
    #      product email one of your users a link to a host they picked. That is
    #      a phishing primitive with our return address on it, and it is worse
    #      than a broken link because it works.
    #
    # Unset outside prod, where the request origin is the local stack and using
    # it keeps `docker compose up` working with no configuration.
    public_base_url: str | None = None

    # The DSAR engine's gateway. The backend calls it rather than the browser,
    # so the request row and the engine call are written in one transaction, the
    # gateway can sit behind internal-only ingress, and the frontend talks to one
    # API instead of two.
    gateway_url: str = "http://fastapi-gateway:8000"
    gateway_timeout_seconds: float = 15.0

    # ------------------------------------------------------------ notifications --
    # Which provider actually sends. `console` logs instead of sending, and is the
    # default everywhere but prod — local development and the test suite must not
    # be able to email a real person about their data.
    notification_provider: str = "console"
    notification_from_address: str | None = None

    # Azure Communication Services
    acs_endpoint: str | None = None
    acs_access_key: str | None = None

    # Generic SMTP, for anyone who already has a relay
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_use_tls: bool = True
    smtp_username: str | None = None
    smtp_password: str | None = None

    cookie_domain: str | None = None
    cookie_secure: bool = True
    refresh_cookie_name: str = "ds_refresh"

    # Brute-force protection on password login.
    max_failed_logins: int = 5
    lockout_minutes: int = 15

    @field_validator("jwt_secret", "audit_hmac_key")
    @classmethod
    def _reject_weak_secrets(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("secrets must be at least 32 characters")
        # Catch the classic "copied the example and shipped it".
        if v.lower().startswith(("changeme", "secret", "example", "test-secret")):
            raise ValueError("refusing to start with a placeholder secret")
        return v

    @model_validator(mode="after")
    def _normalise_paths(self) -> Settings:
        """A malformed prefix produces a cookie the browser silently drops.

        That failure is invisible — no error, no log, just a session that does
        not survive a reload — so it is worth rejecting at boot instead.
        """
        prefix = self.external_path_prefix.strip().rstrip("/")
        if prefix and not prefix.startswith("/"):
            raise ValueError(
                f"external_path_prefix must start with '/' (got {prefix!r})"
            )
        object.__setattr__(self, "external_path_prefix", prefix)
        return self

    @model_validator(mode="after")
    def _prod_guardrails(self) -> Settings:
        if self.env == "prod":
            if self.debug:
                raise ValueError("debug must be off in prod")
            if not self.cookie_secure:
                raise ValueError("cookie_secure must be on in prod")
            if self.notification_provider == "console":
                # In prod, "logged instead of sent" means a statutory
                # notification silently did not happen. Better to refuse to boot
                # than to appear to be notifying people.
                raise ValueError(
                    "notification_provider must not be 'console' in prod — "
                    "statutory notifications would be logged instead of sent"
                )
            if not self.notification_from_address:
                raise ValueError(
                    "notification_from_address is required in prod"
                )
            if "*" in self.cors_origins:
                raise ValueError("wildcard CORS origin is not allowed in prod")
            # Refuse to boot rather than email a link built from a client-supplied
            # Host header. Same reasoning as the console notification provider
            # above: a wrong link that looks right is worse than not starting.
            if not self.public_base_url:
                raise ValueError(
                    "public_base_url is required in prod — emailed links must "
                    "not be derived from the request, which behind internal "
                    "ingress is not even a reachable address"
                )
            # Unencrypted database traffic in production is not a warning-level
            # problem: every row on that wire is personal data.
            if self.db_ssl_mode == "disable":
                raise ValueError(
                    "db_ssl_mode must be 'require' or 'verify-full' in prod — "
                    "unencrypted database connections carry personal data in clear text"
                )
        return self

    @property
    def owner_url(self) -> str:
        """Migration connection. Falls back to the app URL for local dev, where
        the two roles are often the same — but never in prod, where the split is
        what makes RLS meaningful."""
        return str(self.database_owner_url or self.database_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
