"""What this product can connect to, and honestly how far each one goes.

ONE SOURCE OF TRUTH. The admin screen renders itself from this file via
`GET /v1/connections/catalog` — the field list, the labels, the help text, the
status badge. Nothing about a connector is written twice, so the UI cannot offer
a connector the backend has never heard of, and cannot present one as usable when
its status says otherwise. Same reasoning as `frontend/src/config/modules.js`,
applied to integrations.

WHY THE STATUSES EXIST
A page that accepts credentials for forty systems and stores them is easy. A page
that then *connects* to forty systems is forty separate pieces of work, most of
which cannot even be tested without a customer's live account. So each connector
says which it is:

  live         Implemented and verifiable. `probe()` really connects.
  beta         Implemented against the vendor's documentation, never run against
               a real tenant. Do not tell a customer this works.
  planned      Declared so the catalogue is complete and the shape is agreed.
               Storing credentials is refused — there is nothing to send them to.
  needs_oauth  A credentials form is the WRONG interface. These need an app we
               register with the vendor, our client secret, a browser redirect
               and a per-tenant refresh token. The admin clicks Connect; they
               never paste anything. Listed so nobody wires a form to them.
  needs_agent  Unreachable from a cloud service at all. Tally speaks XML on
               localhost to a Windows desktop; no credential makes that routable
               without something running inside the customer's network.

The four `AuthKind`s are not cosmetic either — they are why "how many can we
support by pasting credentials" has an answer smaller than the list of vendors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Status(StrEnum):
    LIVE = "live"
    BETA = "beta"
    PLANNED = "planned"
    NEEDS_OAUTH = "needs_oauth"
    NEEDS_AGENT = "needs_agent"


#: Statuses for which credentials may be stored. Anything else has nowhere to
#: send them, and holding a customer's production secret for a connector that
#: cannot use it is pure liability with no feature attached.
STORABLE = frozenset({Status.LIVE, Status.BETA})


class AuthKind(StrEnum):
    API_KEY = "api_key"          # a token or key/secret pair, pasted
    DATABASE = "database"        # host/port/user/password/database
    SERVICE_ACCOUNT = "service_account"  # a JSON key file, pasted
    OAUTH2 = "oauth2"            # redirect flow — not a form
    AGENT = "agent"              # needs software inside the customer's network


class Capability(StrEnum):
    """What a connector can do once connected — the reason to connect at all."""

    DISCOVER = "discover"        # find where personal data lives
    ACCESS = "access"            # collect a person's data for a §11 request
    ERASE = "erase"              # delete or mask it for a §12 request
    CONSENT_PUSH = "consent_push"  # propagate a withdrawal downstream


@dataclass(frozen=True)
class Field_:
    """One input on the credentials form.

    `secret=True` means the value is encrypted, never returned, and shown back
    only as a last-4 hint.
    """

    key: str
    label: str
    secret: bool = False
    required: bool = True
    placeholder: str = ""
    help: str = ""
    kind: str = "text"           # text | password | number | textarea | select
    options: tuple[str, ...] = ()
    default: str | None = None


@dataclass(frozen=True)
class Connector:
    id: str
    label: str
    category: str
    auth: AuthKind
    status: Status
    fields: tuple[Field_, ...] = ()
    capabilities: tuple[Capability, ...] = ()
    #: Shown in the UI whenever it is set. Required by a test for every connector
    #: that is not `live` — a card that says "not available" must say why.
    note: str = ""
    docs_url: str = ""


# --------------------------------------------------------------------------- #
# Reusable field sets
# --------------------------------------------------------------------------- #

def _db_fields(default_port: int, *, db_label: str = "Database") -> tuple[Field_, ...]:
    return (
        Field_("host", "Host", placeholder="db.internal.example.com",
               help="Must be reachable from this service. A database on a private "
                    "network needs a VPN or private endpoint first — a password "
                    "alone does not make it routable."),
        Field_("port", "Port", kind="number", default=str(default_port)),
        Field_("database", db_label, placeholder="customers"),
        Field_("user", "Username"),
        Field_("password", "Password", secret=True, kind="password"),
        Field_("tls", "Require TLS", kind="select", options=("true", "false"),
               default="true", required=False,
               help="Off only for a database on a trusted private network."),
    )


_ALL_FOUR = (Capability.DISCOVER, Capability.ACCESS, Capability.ERASE)


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #

CONNECTORS: tuple[Connector, ...] = (
    # ---------------------------------------------------------- databases ---
    # The only three that are `live`, and the reason is worth stating: this repo
    # already runs app-postgres, app-mysql and app-mongo as demo datastores, so
    # their probes can be executed and verified rather than merely written.
    Connector(
        id="postgresql", label="PostgreSQL", category="Databases",
        auth=AuthKind.DATABASE, status=Status.LIVE,
        fields=_db_fields(5432), capabilities=_ALL_FOUR,
        docs_url="https://www.postgresql.org/docs/",
    ),
    Connector(
        id="mysql", label="MySQL", category="Databases",
        auth=AuthKind.DATABASE, status=Status.LIVE,
        fields=_db_fields(3306), capabilities=_ALL_FOUR,
    ),
    Connector(
        id="mongodb", label="MongoDB", category="Databases",
        auth=AuthKind.DATABASE, status=Status.LIVE,
        fields=(
            # `srv` is not a nicety — without it this connector cannot reach
            # Atlas, which is how most managed MongoDB is actually deployed.
            # An Atlas cluster is addressed as mongodb+srv://cluster.mongodb.net
            # and resolves to a replica set through SRV records; a bare host and
            # port reaches one node of it at best, and usually nothing.
            Field_("srv", "Atlas / SRV cluster", kind="select",
                   options=("false", "true"), default="false", required=False,
                   help="Turn on for MongoDB Atlas, or any host given to you as "
                        "mongodb+srv://. Leave the port blank when you do."),
            Field_("host", "Host", placeholder="mongo.internal.example.com",
                   help="Must be reachable from this service. For Atlas, the "
                        "cluster hostname without the mongodb+srv:// prefix."),
            Field_("port", "Port", kind="number", default="27017",
                   required=False,
                   help="Ignored for an SRV cluster, which carries its own."),
            Field_("replica_set", "Replica set", required=False,
                   help="Only for a self-hosted replica set addressed by host."),
            Field_("database", "Database", placeholder="customers"),
            Field_("user", "Username", required=False),
            Field_("password", "Password", secret=True, kind="password",
                   required=False),
            Field_("auth_source", "Auth database", required=False,
                   default="admin",
                   help="Where the user is defined. Usually 'admin'."),
            Field_("tls", "Require TLS", kind="select",
                   options=("true", "false"), default="true", required=False),
        ),
        capabilities=_ALL_FOUR,
    ),
    Connector(
        id="firebase", label="Firebase", category="Databases",
        auth=AuthKind.SERVICE_ACCOUNT, status=Status.PLANNED,
        note="Needs a service-account JSON and a decision on Firestore vs "
             "Realtime Database — they have different erasure semantics.",
        fields=(
            Field_("service_account_json", "Service account JSON", secret=True,
                   kind="textarea",
                   help="The whole JSON key file from the Firebase console."),
            Field_("project_id", "Project id"),
        ),
        capabilities=_ALL_FOUR,
    ),
    Connector(
        id="snowflake", label="Snowflake", category="Warehouses",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        note="Credentials fit a form, but erasure in a warehouse usually means "
             "rewriting a table, not deleting a row. Needs a design decision.",
        fields=(
            Field_("account", "Account identifier", placeholder="ab12345.ap-south-1"),
            Field_("user", "Username"),
            Field_("password", "Password", secret=True, kind="password"),
            Field_("warehouse", "Warehouse"),
            Field_("database", "Database"),
            # Snowflake privileges hang off the ROLE, not the user. A session
            # that does not set one gets the user's default, which is often
            # PUBLIC and can see nothing — the connection succeeds and every
            # query returns empty, which is the worst way for this to fail.
            Field_("role", "Role", required=False,
                   help="The role that actually holds the grants. Without it "
                        "Snowflake uses your default, which often sees nothing."),
            Field_("schema", "Schema", required=False, default="PUBLIC"),
        ),
        capabilities=(Capability.DISCOVER, Capability.ACCESS),
    ),
    Connector(
        id="bigquery", label="BigQuery", category="Warehouses",
        auth=AuthKind.SERVICE_ACCOUNT, status=Status.PLANNED,
        note="Same warehouse problem as Snowflake, plus per-dataset scoping.",
        fields=(
            Field_("service_account_json", "Service account JSON", secret=True,
                   kind="textarea"),
            Field_("project_id", "Project id"),
        ),
        capabilities=(Capability.DISCOVER, Capability.ACCESS),
    ),

    # ------------------------------------------------------------- payments ---
    Connector(
        id="razorpay", label="Razorpay", category="Payments",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        note="Erasure is limited by law here, not by the API: transaction "
             "records are retained under RBI rules, so this can realistically "
             "offer access and masking, not deletion.",
        fields=(
            Field_("key_id", "Key ID", placeholder="rzp_live_..."),
            Field_("key_secret", "Key secret", secret=True, kind="password"),
        ),
        capabilities=(Capability.DISCOVER, Capability.ACCESS),
        docs_url="https://razorpay.com/docs/api/",
    ),
    Connector(
        id="cashfree", label="Cashfree", category="Payments",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        note="Same statutory retention constraint as Razorpay.",
        fields=(
            Field_("app_id", "App ID"),
            Field_("secret_key", "Secret key", secret=True, kind="password"),
            Field_("environment", "Environment", kind="select",
                   options=("production", "sandbox"), default="production",
                   help="Sandbox and production have different base URLs and\n"
                        "different keys. Getting this wrong looks like an auth\n"
                        "failure."),
        ),
        capabilities=(Capability.DISCOVER, Capability.ACCESS),
    ),
    Connector(
        id="payu", label="PayU", category="Payments",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        note="Same statutory retention constraint as Razorpay.",
        fields=(
            Field_("merchant_key", "Merchant key"),
            Field_("salt", "Salt", secret=True, kind="password"),
            Field_("environment", "Environment", kind="select",
                   options=("production", "sandbox"), default="production",
                   help="Sandbox and production have different base URLs and\n"
                        "different keys. Getting this wrong looks like an auth\n"
                        "failure."),
        ),
        capabilities=(Capability.DISCOVER, Capability.ACCESS),
    ),
    Connector(
        id="phonepe", label="PhonePe", category="Payments",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        note="Same statutory retention constraint as Razorpay.",
        fields=(
            Field_("merchant_id", "Merchant ID"),
            Field_("salt_key", "Salt key", secret=True, kind="password"),
            Field_("salt_index", "Salt index", default="1"),
            Field_("environment", "Environment", kind="select",
                   options=("production", "sandbox"), default="production",
                   help="Sandbox and production have different base URLs and\n"
                        "different keys. Getting this wrong looks like an auth\n"
                        "failure."),
        ),
        capabilities=(Capability.DISCOVER, Capability.ACCESS),
    ),

    # ----------------------------------------------------- support / CRM ---
    Connector(
        id="freshdesk", label="Freshdesk", category="Support",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        fields=(
            Field_("domain", "Domain", placeholder="yourcompany.freshdesk.com"),
            Field_("api_key", "API key", secret=True, kind="password"),
        ),
        capabilities=_ALL_FOUR,
    ),
    Connector(
        id="freshsales", label="Freshsales", category="CRM",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        fields=(
            Field_("domain", "Domain", placeholder="yourcompany.myfreshworks.com"),
            Field_("api_key", "API key", secret=True, kind="password"),
        ),
        capabilities=_ALL_FOUR,
    ),
    Connector(
        id="freshservice", label="Freshservice", category="Support",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        fields=(
            Field_("domain", "Domain", placeholder="yourcompany.freshservice.com"),
            Field_("api_key", "API key", secret=True, kind="password"),
        ),
        capabilities=_ALL_FOUR,
    ),
    Connector(
        id="zendesk", label="Zendesk", category="Support",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        note="Token auth avoids OAuth here. Zendesk also has a native "
             "deletion endpoint, which is worth using rather than reimplementing.",
        fields=(
            Field_("subdomain", "Subdomain", placeholder="yourcompany"),
            Field_("email", "Agent email"),
            Field_("api_token", "API token", secret=True, kind="password"),
        ),
        capabilities=_ALL_FOUR,
    ),
    Connector(
        id="hubspot", label="HubSpot", category="CRM",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        note="A private-app token works without the OAuth dance.",
        fields=(
            Field_("access_token", "Private app token", secret=True,
                   kind="password"),
        ),
        capabilities=_ALL_FOUR,
    ),
    Connector(
        id="salesforce", label="Salesforce", category="CRM",
        auth=AuthKind.OAUTH2, status=Status.NEEDS_OAUTH,
        note="Connected App plus a redirect flow. A JWT bearer flow is possible "
             "but needs a certificate per customer, not a pasted secret.",
        capabilities=_ALL_FOUR,
    ),

    # ----------------------------------------------------------- Zoho ---
    # All three share one OAuth app and one refresh token per tenant.
    Connector(
        id="zoho_crm", label="Zoho CRM", category="CRM",
        auth=AuthKind.OAUTH2, status=Status.NEEDS_OAUTH,
        note="Zoho OAuth, region-specific (.in / .com / .eu). One consent grant "
             "covers CRM, Desk and Books, so these three connect together.",
        capabilities=_ALL_FOUR,
    ),
    Connector(
        id="zoho_desk", label="Zoho Desk", category="Support",
        auth=AuthKind.OAUTH2, status=Status.NEEDS_OAUTH,
        note="Shares the Zoho OAuth grant with CRM and Books.",
        capabilities=_ALL_FOUR,
    ),
    Connector(
        id="zoho_books", label="Zoho Books", category="Finance",
        auth=AuthKind.OAUTH2, status=Status.NEEDS_OAUTH,
        note="Shares the Zoho OAuth grant. Invoices are statutory records, so "
             "expect access and masking rather than erasure.",
        capabilities=(Capability.DISCOVER, Capability.ACCESS),
    ),

    # ------------------------------------------------------- e-commerce ---
    Connector(
        id="shopify", label="Shopify", category="E-commerce",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        note="A custom-app Admin API token avoids OAuth. Shopify also emits its "
             "own GDPR erasure webhooks, which should feed this rather than be "
             "duplicated.",
        fields=(
            Field_("shop_domain", "Shop domain",
                   placeholder="yourstore.myshopify.com"),
            Field_("access_token", "Admin API access token", secret=True,
                   kind="password"),
            # Shopify puts the version in the request path and retires versions
            # on a published schedule. Omitting it means the call either fails
            # or silently follows whatever Shopify currently defaults to, which
            # changes under you.
            Field_("api_version", "Admin API version", default="2024-10",
                   help="Shopify retires versions on a schedule; pin the one "
                        "your app was built against."),
        ),
        capabilities=_ALL_FOUR,
    ),
    Connector(
        id="woocommerce", label="WooCommerce", category="E-commerce",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        fields=(
            Field_("site_url", "Store URL", placeholder="https://store.example.com"),
            Field_("consumer_key", "Consumer key"),
            Field_("consumer_secret", "Consumer secret", secret=True,
                   kind="password"),
        ),
        capabilities=_ALL_FOUR,
    ),
    Connector(
        id="unicommerce", label="Unicommerce", category="E-commerce",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        fields=(
            Field_("tenant_url", "Tenant URL",
                   placeholder="https://yourco.unicommerce.com"),
            Field_("username", "Username"),
            Field_("password", "Password", secret=True, kind="password"),
            Field_("facility", "Facility code", required=False),
        ),
        capabilities=(Capability.DISCOVER, Capability.ACCESS),
    ),
    Connector(
        id="shiprocket", label="Shiprocket", category="Logistics",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        note="Email and password are exchanged for a 10-day token, so this needs "
             "a refresh cycle rather than a static credential.",
        fields=(
            Field_("email", "Account email"),
            Field_("password", "Password", secret=True, kind="password"),
        ),
        capabilities=(Capability.DISCOVER, Capability.ACCESS),
    ),
    Connector(
        id="delhivery", label="Delhivery", category="Logistics",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        fields=(
            Field_("api_token", "API token", secret=True, kind="password"),
            Field_("environment", "Environment", kind="select",
                   options=("production", "sandbox"), default="production",
                   help="Sandbox and production have different base URLs and\n"
                        "different keys. Getting this wrong looks like an auth\n"
                        "failure."),
        ),
        capabilities=(Capability.DISCOVER, Capability.ACCESS),
    ),

    # ------------------------------------------------ messaging / telephony ---
    Connector(
        id="whatsapp_business", label="WhatsApp Business", category="Messaging",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        note="Via Meta's Cloud API. A permanent token belongs to a system user, "
             "not a person — worth getting right at setup.",
        fields=(
            Field_("phone_number_id", "Phone number ID"),
            Field_("access_token", "Permanent access token", secret=True,
                   kind="password"),
            # Needed for anything account-scoped rather than message-scoped,
            # including reading the message templates a consent notice uses.
            Field_("business_account_id", "WhatsApp Business Account ID",
                   required=False,
                   help="The WABA id. Required to read or manage templates."),
        ),
        capabilities=(Capability.CONSENT_PUSH,),
    ),
    Connector(
        id="interakt", label="Interakt", category="Messaging",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        fields=(Field_("api_key", "API key", secret=True, kind="password"),),
        capabilities=(Capability.CONSENT_PUSH,),
    ),
    Connector(
        id="gupshup", label="Gupshup", category="Messaging",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        fields=(
            Field_("api_key", "API key", secret=True, kind="password"),
            Field_("app_name", "App name"),
        ),
        capabilities=(Capability.CONSENT_PUSH,),
    ),
    Connector(
        id="exotel", label="Exotel", category="Telephony",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        note="Call recordings are personal data and often the largest store of "
             "it a company forgets about.",
        fields=(
            # Exotel authenticates with an API KEY and API TOKEN pair as HTTP
            # Basic credentials, with the SID only identifying the account in
            # the URL path. The key was missing here, so these fields could not
            # have authenticated anything.
            Field_("sid", "Account SID",
                   help="Identifies the account in the request path."),
            Field_("api_key", "API key",
                   help="The username half of the Basic credential."),
            Field_("api_token", "API token", secret=True, kind="password",
                   help="The password half."),
            Field_("subdomain", "Subdomain", default="api.exotel.com",
                   help="api.exotel.com, or api.in.exotel.com for the "
                        "Singapore/India cluster."),
        ),
        capabilities=(Capability.DISCOVER, Capability.ACCESS, Capability.ERASE),
    ),
    Connector(
        id="knowlarity", label="Knowlarity", category="Telephony",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        fields=(
            Field_("api_key", "API key", secret=True, kind="password"),
            Field_("sr_number", "SR number", required=False),
        ),
        capabilities=(Capability.DISCOVER, Capability.ACCESS),
    ),

    # ----------------------------------------------------------- finance ---
    Connector(
        id="tally", label="Tally", category="Finance",
        auth=AuthKind.AGENT, status=Status.NEEDS_AGENT,
        note="Tally is a Windows desktop application that speaks XML over HTTP "
             "on localhost. No credential makes it reachable from a cloud "
             "service — it needs a small agent running inside the customer's "
             "network. Listed so nobody attaches a credentials form to it.",
        capabilities=(Capability.DISCOVER, Capability.ACCESS),
    ),
    Connector(
        id="khatabook", label="Khatabook", category="Finance",
        auth=AuthKind.AGENT, status=Status.NEEDS_AGENT,
        note="No public API to integrate against. Nothing to build until one "
             "exists or the customer can export.",
    ),

    # ------------------------------------------- workspace / collaboration ---
    Connector(
        id="google_workspace", label="Google Workspace", category="Workspace",
        auth=AuthKind.OAUTH2, status=Status.NEEDS_OAUTH,
        note="Domain-wide delegation, which an admin grants in their Google "
             "console — not something pasted here.",
        capabilities=(Capability.DISCOVER, Capability.ACCESS),
    ),
    Connector(
        id="google_drive", label="Google Drive", category="Workspace",
        auth=AuthKind.OAUTH2, status=Status.NEEDS_OAUTH,
        note="Shares the Google Workspace grant.",
        capabilities=(Capability.DISCOVER, Capability.ACCESS),
    ),
    Connector(
        id="microsoft_365", label="Microsoft 365", category="Workspace",
        auth=AuthKind.OAUTH2, status=Status.NEEDS_OAUTH,
        note="Entra app with admin consent, via Microsoft Graph.",
        capabilities=(Capability.DISCOVER, Capability.ACCESS),
    ),
    Connector(
        id="sharepoint", label="Microsoft SharePoint", category="Workspace",
        auth=AuthKind.OAUTH2, status=Status.NEEDS_OAUTH,
        note="Shares the Microsoft Graph grant with M365.",
        capabilities=(Capability.DISCOVER, Capability.ACCESS),
    ),
    Connector(
        id="slack", label="Slack", category="Collaboration",
        auth=AuthKind.OAUTH2, status=Status.NEEDS_OAUTH,
        note="Slack app install flow. Message history is personal data and "
             "erasure there is genuinely hard.",
        capabilities=(Capability.DISCOVER, Capability.ACCESS),
    ),
    Connector(
        id="microsoft_teams", label="Microsoft Teams", category="Collaboration",
        auth=AuthKind.OAUTH2, status=Status.NEEDS_OAUTH,
        note="Shares the Microsoft Graph grant.",
        capabilities=(Capability.DISCOVER, Capability.ACCESS),
    ),

    # -------------------------------------------------------------- cloud ---
    # Deliberately one card each, with a note, rather than pretending "connect
    # AWS" is a single connection.
    Connector(
        id="aws", label="AWS", category="Cloud",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        note="Not one connection. Personal data could be in S3, RDS, DynamoDB "
             "or Redshift, each needing its own handling. Prefer a cross-account "
             "role over a pasted access key — a long-lived key with broad "
             "permissions is the worst credential to hold on a customer's behalf.",
        fields=(
            Field_("role_arn", "Role ARN", required=False,
                   placeholder="arn:aws:iam::123456789012:role/DataShieldAccess",
                   help="Preferred. Leave the keys blank if you use this."),
            # Without an external id, a cross-account role that trusts our
            # account can be assumed on behalf of ANY customer who knows the
            # ARN — the confused-deputy problem AWS documents this field to
            # solve. It is not optional in practice, only in the API.
            Field_("external_id", "External ID", required=False, secret=True,
                   kind="password",
                   help="Required with a Role ARN. Without it, anyone who "
                        "learns the ARN can have it assumed on their behalf."),
            Field_("access_key_id", "Access key ID", required=False),
            Field_("secret_access_key", "Secret access key", secret=True,
                   kind="password", required=False),
            Field_("region", "Region", default="ap-south-1"),
        ),
        capabilities=(Capability.DISCOVER,),
    ),
    Connector(
        id="azure", label="Microsoft Azure", category="Cloud",
        auth=AuthKind.API_KEY, status=Status.PLANNED,
        note="Same caveat as AWS. A service principal scoped to one resource "
             "group, never a subscription-wide one.",
        fields=(
            Field_("tenant_id", "Directory (tenant) ID"),
            Field_("client_id", "Application (client) ID"),
            Field_("client_secret", "Client secret", secret=True, kind="password"),
            Field_("subscription_id", "Subscription ID"),
        ),
        capabilities=(Capability.DISCOVER,),
    ),
    Connector(
        id="gcp", label="Google Cloud", category="Cloud",
        auth=AuthKind.SERVICE_ACCOUNT, status=Status.PLANNED,
        note="Same caveat as AWS. Scope the service account to the projects that "
             "actually hold personal data.",
        fields=(
            Field_("service_account_json", "Service account JSON", secret=True,
                   kind="textarea"),
            Field_("project_id", "Project id"),
        ),
        capabilities=(Capability.DISCOVER,),
    ),
)


BY_ID: dict[str, Connector] = {c.id: c for c in CONNECTORS}


def get(connector_id: str) -> Connector | None:
    return BY_ID.get(connector_id)


def storable(connector_id: str) -> bool:
    """Whether credentials may be stored for this connector at all."""
    c = BY_ID.get(connector_id)
    return bool(c and c.status in STORABLE)


def as_catalog() -> list[dict]:
    """The catalogue, in the shape the admin screen renders.

    Secret fields carry `secret: true` so the form knows to mask them, but no
    value ever travels in this payload — it describes the *shape* of a
    credential, never one.
    """
    return [
        {
            "id": c.id,
            "label": c.label,
            "category": c.category,
            "auth": c.auth.value,
            "status": c.status.value,
            "storable": c.status in STORABLE,
            "note": c.note,
            "docs_url": c.docs_url,
            "capabilities": [cap.value for cap in c.capabilities],
            "fields": [
                {
                    "key": f.key, "label": f.label, "secret": f.secret,
                    "required": f.required, "placeholder": f.placeholder,
                    "help": f.help, "kind": f.kind,
                    "options": list(f.options), "default": f.default,
                }
                for f in c.fields
            ],
        }
        for c in CONNECTORS
    ]
