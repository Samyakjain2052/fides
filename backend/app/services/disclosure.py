"""Assembling what an access request is actually answered with.

§11 gives a person the right to a summary of their personal data and of the
processing done on it. Until now the only way to satisfy that here was a live
JSON passthrough from the engine — which meant a request handled through the
connections and data-map path produced nothing at all, the response was not
something anybody could keep, and the engine had to retain the data
indefinitely for the endpoint to keep working. This assembles a real artifact
once, stores it, and hashes it.

TWO VIEWS OF THE SAME DATA, AND WHY THEY DIFFER

  the person   everything. It is their data; that is the whole right.
  the admin    field names, locations, and the values that do not identify a
               household or an account. Everything else reads
               <SENSITIVE VALUE EXCLUDED>.

The second one is the point. An administrator has to be able to confirm the
match is the right person and that the package is not obviously wrong, and has
no business reading somebody's Aadhaar number to do it. A rights request
authorises acting on data, not browsing it — the same reasoning `data_map_service`
already follows for discovery, extended to the one place where values genuinely
have to be handled.

So `preview` is what a screen renders and `assemble` is what gets delivered, and
they are deliberately not the same function. Staff cannot download the assembled
package at all — see `file_service._STAFF_CAPABILITY`, which grants no staff
capability over `dsar_package`. If that seems severe, the alternative is a
product where every DPO can read every customer's financial records by raising a
request on their behalf.

WHY THE ZIP IS NOT PASSWORD-PROTECTED

Python writes only legacy ZipCrypto, which is broken and has been for decades. A
password on it would be worse than none, because it *looks* like protection.
The real controls are the ones that work: the object is encrypted at rest with
AES-256-GCM, served over TLS from an authenticated endpoint that only the data
principal it belongs to can call, and it expires. If a customer needs an
encrypted archive they can hold, that is an argument for AES-256 zip support via
a real library, not for pretending.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from datetime import UTC, datetime
from typing import Any

from app.connectors.discovery import CATEGORY_PATTERNS, _matches

logger = logging.getLogger("app.disclosure")

#: What an administrator does not get to read.
#:
#: These are the categories where seeing the value serves no operational purpose
#: and getting it wrong is a serious harm. "Financial" covers card and account
#: numbers; note that it also matches `amount`, which is over-broad in the
#: cautious direction and stays that way — an admin does not need to read
#: somebody's transaction amounts to verify a disclosure either.
SENSITIVE_CATEGORIES = ("Government ID", "Financial", "Health")

#: Location precise enough to identify a household, redacted separately from the
#: coarse contact fields.
#:
#: The line is granularity, not field type: `city` and `country` are shown
#: because they help an admin confirm they have the right person and identify
#: nobody on their own, while a street address or a postcode narrows to a front
#: door. Latitude and longitude are a front door with extra steps.
PRECISE_LOCATION_PATTERNS = (
    r"street", r"address_?line", r"^address$", r"_address$",
    r"pin_?code", r"postal", r"^zip", r"latitude", r"longitude",
    r"coordinates", r"^geo",
)

#: Never disclosed to anyone, in either view.
#:
#: A password hash is not the person's personal data in any useful sense, it is
#: our credential material about them, and putting one in a disclosure package
#: hands an attacker who social-engineers a rights request something to crack
#: offline. Same for tokens and secrets.
NEVER_DISCLOSE_PATTERNS = (
    r"password", r"passwd", r"secret", r"token", r"api_?key",
    r"private_?key", r"salt", r"hash$", r"_hash$", r"session_?id",
)

EXCLUDED = "<SENSITIVE VALUE EXCLUDED>"
WITHHELD = "<WITHHELD — CREDENTIAL MATERIAL>"


def classification(field: str) -> str:
    """`never`, `sensitive`, or `ordinary` for one column name.

    Order matters. Credential material is checked first so a column called
    `payment_token` is withheld rather than merely marked sensitive, and precise
    location before the generic Contact patterns so `street_address` does not
    fall through as ordinary contact detail.
    """
    name = (field or "").lower()

    if _matches(name, NEVER_DISCLOSE_PATTERNS):
        return "never"
    for category in SENSITIVE_CATEGORIES:
        if _matches(name, CATEGORY_PATTERNS.get(category, ())):
            return "sensitive"
    if _matches(name, PRECISE_LOCATION_PATTERNS):
        return "sensitive"
    return "ordinary"


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def flatten(data: dict[str, Any]) -> list[dict[str, str]]:
    """Engine output into flat (collection, field, value) rows.

    The engine returns `{collection: [record, ...]}`, and records nest. Flattened
    with dotted paths so a CSV can hold the whole thing and a human can read it —
    nested JSON in a spreadsheet cell is technically a disclosure and practically
    an insult.
    """
    rows: list[dict[str, str]] = []

    def walk(collection: str, prefix: str, node: Any, index: int | None) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(collection, f"{prefix}.{key}" if prefix else key, value, index)
        elif isinstance(node, list):
            for position, item in enumerate(node):
                walk(collection, prefix, item, position)
        else:
            rows.append({
                "collection": collection,
                "record": "" if index is None else str(index + 1),
                "field": prefix,
                "value": _stringify(node),
            })

    for collection, payload in (data or {}).items():
        walk(str(collection), "", payload, None)
    return rows


def preview(data: dict[str, Any]) -> dict[str, Any]:
    """What an administrator sees. Sensitive values excluded, not merely masked.

    Excluded rather than partially shown: the last four digits of an account
    number is not a redaction, it is most of an account number, and `mask` exists
    for credentials an admin typed themselves rather than for somebody else's
    identifiers.
    """
    rows = flatten(data)
    out = []
    counts = {"ordinary": 0, "sensitive": 0, "never": 0}

    for row in rows:
        kind = classification(row["field"])
        counts[kind] += 1
        if kind == "never":
            shown = WITHHELD
        elif kind == "sensitive":
            shown = EXCLUDED
        else:
            shown = row["value"]
        out.append({**row, "value": shown, "classification": kind})

    return {
        "rows": out,
        "field_count": len(rows),
        "collections": sorted({r["collection"] for r in rows}),
        "counts": counts,
    }


def _readme(*, reference: str, produced_at: datetime, collections: list[str]) -> str:
    """Plain language, because a zip of CSVs is not a disclosure to most people.

    §11 is a right to a summary, and a summary somebody cannot read has not been
    provided. No legal citations in the body beyond the one that explains what
    they are holding.
    """
    listed = "\n".join(f"  - {c}" for c in collections) or "  (none found)"
    return (
        f"Your personal data\n"
        f"==================\n\n"
        f"Request reference: {reference}\n"
        f"Prepared on:       {produced_at:%d %B %Y at %H:%M UTC}\n\n"
        f"This package was produced in response to your request to see the\n"
        f"personal data held about you. It contains everything we found.\n\n"
        f"WHAT IS IN HERE\n\n"
        f"  summary.csv   Every field we hold, where it is held, and its value.\n"
        f"                Open it with any spreadsheet program.\n"
        f"  data.json     The same information, structured, for anyone who wants\n"
        f"                to process it with software.\n"
        f"  Any other files are documents that were attached to your request.\n\n"
        f"WHERE IT CAME FROM\n\n{listed}\n\n"
        f"IF SOMETHING HERE IS WRONG\n\n"
        f"You can ask us to correct or complete it, or to erase it. You do not\n"
        f"have to explain why. If you are not satisfied with how we handled this\n"
        f"request you can raise a grievance with us, and if that does not resolve\n"
        f"it you may approach the Data Protection Board of India.\n\n"
        f"A NOTE ON KEEPING THIS SAFE\n\n"
        f"This file contains your personal data in full. Anyone who obtains it\n"
        f"can read all of it. Store it somewhere you control.\n"
    )


def build_zip(
    *,
    reference: str,
    data: dict[str, Any],
    attachments: list[tuple[str, bytes]] | None = None,
    produced_at: datetime | None = None,
) -> bytes:
    """The artifact itself. Deterministic apart from the timestamp.

    Fixed member timestamps so two packages built from identical data hash
    identically — which is what makes `sha256` on the stored file evidence of
    anything. Zip stores mtimes, and letting them default to "now" would produce
    a different digest every run for the same disclosure.
    """
    moment = produced_at or datetime.now(UTC)
    rows = flatten(data)
    collections = sorted({r["collection"] for r in rows})

    buffer = io.BytesIO()
    # ZIP_DEFLATED: these are CSV and JSON, which compress well, and a person on
    # a phone connection is downloading their whole life.
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:

        def write(name: str, payload: bytes) -> None:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            # 0644. Without an explicit mode, members extract with whatever the
            # platform guesses, which on some tools is 000.
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload)

        write("README.txt", _readme(
            reference=reference, produced_at=moment, collections=collections,
        ).encode("utf-8"))

        csv_buffer = io.StringIO()
        writer = csv.DictWriter(
            csv_buffer, fieldnames=["collection", "record", "field", "value"]
        )
        writer.writeheader()
        for row in rows:
            if classification(row["field"]) == "never":
                # Withheld from the person too. It is our credential material
                # about them, not information about them, and a password hash in
                # a disclosure package is something to crack offline.
                writer.writerow({**row, "value": WITHHELD})
            else:
                writer.writerow(row)
        # BOM so Excel opens UTF-8 without mangling non-ASCII names, which in
        # India is most names.
        write("summary.csv", csv_buffer.getvalue().encode("utf-8-sig"))

        write("data.json", json.dumps(
            _scrub(data), indent=2, ensure_ascii=False, default=str
        ).encode("utf-8"))

        for name, payload in (attachments or []):
            # Namespaced so an attachment called `summary.csv` cannot displace
            # the real one.
            write(f"attachments/{name}", payload)

    return buffer.getvalue()


def _scrub(node: Any) -> Any:
    """Recursively withhold credential material from the structured copy."""
    if isinstance(node, dict):
        return {
            key: (WITHHELD if classification(str(key)) == "never" else _scrub(value))
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_scrub(item) for item in node]
    return node
