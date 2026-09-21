"""Find an account's mail servers from its address alone.

The user wants to answer the person who wrote to them, from the address the
mail arrived at. They do not know, and should not have to know, what IMAP or
SMTP is, which port their provider uses, or whether it wants STARTTLS. Every
mature client works this out from the address; this follows Thunderbird's
order, because it is the one with a published specification
(draft-bucksch-autoconfig) and the largest shared database behind it:

  1. the provider's own file:   https://autoconfig.<domain>/mail/config-v1.1.xml
  2. its well-known location:   https://<domain>/.well-known/autoconfig/mail/config-v1.1.xml
  3. the shared database:       https://autoconfig.thunderbird.net/v1.1/<domain>
  4. the domain's MX host, looked up in the shared database -- this is what
     turns "a university domain" into "Microsoft 365" or "Google Workspace"
  5. conventional names:        imap.<domain>, smtp.<domain>

Privacy, stated rather than implied: steps 1-2 go to the user's own provider.
Step 3 sends the **domain** -- never the address -- to Mozilla's database.
Step 4 asks the local resolver (`dig`), not a third-party DNS service.

Nothing here is specific to one provider or one machine. There is no table of
hand-typed hostnames to go stale: the database is the table.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from typing import Callable
from urllib.parse import quote

TIMEOUT = 6.0

ISPDB = "https://autoconfig.thunderbird.net/v1.1/{domain}"
PROVIDER = "https://autoconfig.{domain}/mail/config-v1.1.xml?emailaddress={address}"
WELL_KNOWN = "https://{domain}/.well-known/autoconfig/mail/config-v1.1.xml"

SECURITY = {"SSL": "ssl", "TLS": "ssl", "STARTTLS": "starttls", "plain": "plain"}


@dataclass(frozen=True)
class ServerConfig:
    imap_host: str
    imap_port: int
    imap_security: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    imap_username: str
    smtp_username: str
    source: str                 # which step found it -- shown to the user on failure
    oauth_only: bool = False    # the provider accepts no password at all

    def as_dict(self) -> dict:
        return asdict(self)


Fetcher = Callable[[str], "str | None"]
MxLookup = Callable[[str], list]


def _fetch(url: str) -> str | None:
    """GET, returning the body or None. Never raises: a missing file at one
    step is the normal case, not an error."""
    import urllib.request
    request = urllib.request.Request(url, headers={"User-Agent": "FoolsGold/1 (autoconfig)"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
            if reply.status != 200:
                return None
            return reply.read(512 * 1024).decode("utf-8", "replace")
    except Exception:                                    # noqa: BLE001
        return None


def _mx(domain: str) -> list[str]:
    """MX hosts, best first, from the system resolver. Empty when there is no
    `dig` -- a missing tool must cost this one step, not the whole search."""
    dig = shutil.which("dig")
    if not dig:
        return []
    try:
        out = subprocess.run([dig, "+short", "+time=3", "+tries=1", "MX", domain],
                             capture_output=True, text=True, timeout=8).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            rows.append((int(parts[0]), parts[1].rstrip(".").lower()))
    return [host for _, host in sorted(rows)]


def _fill(template: str, address: str) -> str:
    local, _, domain = address.partition("@")
    return (template.replace("%EMAILADDRESS%", address)
                    .replace("%EMAILLOCALPART%", local)
                    .replace("%EMAILDOMAIN%", domain))


def parse(xml_text: str, address: str, source: str) -> ServerConfig | None:
    """The config-v1.1 format. IMAP over POP, encrypted over plain, and the
    first outgoing server; a provider that lists only OAuth2 is reported as
    such rather than handed a password it will refuse."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    provider = root.find("emailProvider")
    if provider is None:
        return None

    def servers(tag: str, kind: str):
        found = []
        for node in provider.findall(tag):
            if (node.get("type") or "").lower() != kind:
                continue
            host = (node.findtext("hostname") or "").strip()
            port = (node.findtext("port") or "").strip()
            sock = SECURITY.get((node.findtext("socketType") or "").strip(), "")
            auths = {(a.text or "").strip() for a in node.findall("authentication")}
            user = _fill((node.findtext("username") or "%EMAILADDRESS%").strip(), address)
            if host and port.isdigit() and sock:
                found.append((host, int(port), sock, user, auths))
        # Encrypted first; the file lists alternatives in no promised order.
        return sorted(found, key=lambda s: {"ssl": 0, "starttls": 1}.get(s[2], 2))

    imap = servers("incomingServer", "imap")
    smtp = servers("outgoingServer", "smtp")
    if not imap or not smtp:
        return None
    i, o = imap[0], smtp[0]
    password_ok = {"password-cleartext", "password-encrypted", "plain", "secure"}
    oauth_only = not (i[4] & password_ok) and "OAuth2" in i[4]
    return ServerConfig(i[0], i[1], i[2], o[0], o[1], o[2], i[3], o[3], source, oauth_only)


def _base_domain(host: str) -> str:
    """`aspmx.l.google.com` -> `google.com`. Two labels, or three for the
    handful of second-level registries (co.uk, ac.kr, edu.hk...)."""
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-2]) <= 3 and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def discover(address: str, *, fetch: Fetcher = _fetch, mx: MxLookup = _mx) -> ServerConfig | None:
    address = (address or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
        return None
    domain = address.rsplit("@", 1)[1]

    for source, url in (
        ("provider", PROVIDER.format(domain=domain, address=quote(address))),
        ("well-known", WELL_KNOWN.format(domain=domain)),
        ("ispdb", ISPDB.format(domain=domain)),
    ):
        body = fetch(url)
        found = parse(body, address, source) if body else None
        if found:
            return found

    # A university or company domain rarely has its own entry, but its MX host
    # belongs to whoever actually runs the mail -- and they do.
    for host in mx(domain)[:2]:
        base = _base_domain(host)
        if base == domain:
            continue
        body = fetch(ISPDB.format(domain=base))
        found = parse(body, address, f"mx:{base}") if body else None
        if found:
            return found

    # Last resort, and marked as a guess so the first failed login can say so.
    return ServerConfig(f"imap.{domain}", 993, "ssl", f"smtp.{domain}", 465, "ssl",
                        address, address, "guess")
