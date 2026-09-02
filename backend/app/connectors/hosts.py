"""Where a connector is allowed to connect.

A connection test is, by construction, a request to a host somebody else chose.
That makes it a server-side request forgery primitive unless it is fenced, and
this deployment made the consequence concrete: the container apps sit in a VNet
whose data subnet holds a private endpoint to *our own* PostgreSQL server. A
tenant who added a "customer database" pointing at that private address got back

    password authentication failed for user "datashield_app"

— which confirms the server is there, confirms the role name is real (PostgreSQL
distinguishes a bad role from a bad password), and turns the Test button into a
password-guessing oracle against our production database from inside our own
network, past the firewall. Registration is open, so anybody could do it.

So the reachable set is narrowed to public addresses. That costs nothing real:
a customer's database on a private network is not reachable from Azure anyway
without a VPN or peering, so refusing it here removes an attack surface without
removing a capability. Local development needs the opposite, because the demo
datastores live on a Docker bridge network — hence the setting, defaulting to
the safe answer.

RESIDUAL RISK, stated rather than hidden: this resolves the name, checks every
address, and then lets the driver connect by name. A DNS record that changes
between those two steps could still point at a private address. Closing that
means connecting to the validated IP, which breaks TLS hostname verification for
the drivers that do verify. The rebinding window is milliseconds and needs an
attacker-controlled authoritative server with a near-zero TTL; typing a private
address, or a hostname that resolves to one, is the realistic attack and is now
refused outright.
"""

from __future__ import annotations

import ipaddress
import socket

from app.core.config import get_settings


class HostNotAllowed(Exception):
    """The host is outside the set a connector may reach."""


def _classify(addr: str) -> str | None:
    """Why this address is refused, or None if it is fine."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return None  # not an address; the caller resolves names separately

    if ip.is_loopback:
        return "a loopback address"
    if ip.is_link_local:
        # 169.254.0.0/16 is where cloud instance-metadata services live. On
        # Azure that is 169.254.169.254, which hands out managed-identity
        # tokens — the single most valuable thing an SSRF can reach here.
        return "a link-local address (this is where cloud metadata services live)"
    if ip.is_private:
        return "a private address"
    if ip.is_multicast:
        return "a multicast address"
    if ip.is_reserved or ip.is_unspecified:
        return "a reserved address"
    return None


def resolve_and_check(host: str, port: int) -> list[str]:
    """Resolve `host` and refuse it unless every address is publicly routable.

    Returns the resolved addresses, so a caller that wants to log what it
    checked can. Raises `HostNotAllowed` with a reason an admin can act on.

    EVERY address is checked, not just the first. A name with both a public and
    a private A record would otherwise pass the check and connect to whichever
    the driver picked.
    """
    name = (host or "").strip()
    if not name:
        raise HostNotAllowed("No host given.")

    if get_settings().connector_allow_private_hosts:
        return []

    # A literal address needs no lookup, and checking it first means an obvious
    # attempt is refused without emitting a DNS query at all.
    direct = _classify(name)
    if direct:
        raise HostNotAllowed(
            f"{name} is {direct}, which this service will not connect to. "
            "A database on a private network is not reachable from here in any "
            "case — it needs a VPN or private peering first."
        )

    try:
        infos = socket.getaddrinfo(name, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise HostNotAllowed(f"{name} could not be resolved ({exc.strerror or exc}).") from exc

    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise HostNotAllowed(f"{name} resolved to no addresses.")

    for addr in addresses:
        why = _classify(addr)
        if why:
            raise HostNotAllowed(
                f"{name} resolves to {addr}, which is {why}. This service will "
                "not connect to it."
            )
    return addresses
