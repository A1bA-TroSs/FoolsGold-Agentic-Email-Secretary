"""Server discovery from an address, against a fake network.

Each test hands `discover` a fetcher that knows exactly which URLs exist, so
the order of the search -- which is the whole design -- is what is asserted.
"""
from __future__ import annotations

from app.transports import autoconfig as A

GMAIL_XML = """<?xml version="1.0"?>
<clientConfig version="1.1"><emailProvider id="googlemail.com">
  <incomingServer type="pop3"><hostname>pop.gmail.com</hostname><port>995</port>
    <socketType>SSL</socketType><username>%EMAILADDRESS%</username>
    <authentication>password-cleartext</authentication></incomingServer>
  <incomingServer type="imap"><hostname>imap.gmail.com</hostname><port>993</port>
    <socketType>SSL</socketType><username>%EMAILADDRESS%</username>
    <authentication>OAuth2</authentication>
    <authentication>password-cleartext</authentication></incomingServer>
  <outgoingServer type="smtp"><hostname>smtp.gmail.com</hostname><port>465</port>
    <socketType>SSL</socketType><username>%EMAILADDRESS%</username>
    <authentication>password-cleartext</authentication></outgoingServer>
</emailProvider></clientConfig>"""

LOCALPART_XML = GMAIL_XML.replace("imap.gmail.com", "imap.example.org") \
    .replace("%EMAILADDRESS%</username>\n    <authentication>OAuth2",
             "%EMAILLOCALPART%</username>\n    <authentication>OAuth2")

OAUTH_ONLY_XML = """<clientConfig><emailProvider id="x">
  <incomingServer type="imap"><hostname>outlook.office365.com</hostname><port>993</port>
    <socketType>SSL</socketType><username>%EMAILADDRESS%</username>
    <authentication>OAuth2</authentication></incomingServer>
  <outgoingServer type="smtp"><hostname>smtp.office365.com</hostname><port>587</port>
    <socketType>STARTTLS</socketType><username>%EMAILADDRESS%</username>
    <authentication>OAuth2</authentication></outgoingServer>
</emailProvider></clientConfig>"""


def web(pages: dict):
    asked = []

    def fetch(url):
        asked.append(url)
        return pages.get(url)
    fetch.asked = asked
    return fetch


def test_the_providers_own_file_wins():
    fetch = web({"https://autoconfig.example.org/mail/config-v1.1.xml?emailaddress=me%40example.org":
                 GMAIL_XML})
    got = A.discover("me@example.org", fetch=fetch, mx=lambda d: [])
    assert got.source == "provider"
    assert (got.imap_host, got.imap_port, got.imap_security) == ("imap.gmail.com", 993, "ssl")
    assert len(fetch.asked) == 1, "a hit must end the search"


def test_the_shared_database_is_asked_by_domain_never_by_address():
    """Privacy, asserted rather than promised: Mozilla's database learns which
    domain, not who."""
    fetch = web({"https://autoconfig.thunderbird.net/v1.1/example.org": GMAIL_XML})
    got = A.discover("danny.park@example.org", fetch=fetch, mx=lambda d: [])
    assert got.source == "ispdb"
    ispdb = [u for u in fetch.asked if "thunderbird.net" in u]
    assert ispdb and all("danny" not in u for u in ispdb)


def test_a_university_domain_is_resolved_through_its_mx_host():
    """The case this exists for: nobody publishes a config for ust.hk, but its
    MX points at Microsoft, and Microsoft's entry is in the database."""
    fetch = web({"https://autoconfig.thunderbird.net/v1.1/outlook.com": OAUTH_ONLY_XML})
    got = A.discover("s123@connect.ust.hk", fetch=fetch,
                     mx=lambda d: ["connect-ust-hk.mail.protection.outlook.com"])
    assert got.source == "mx:outlook.com"
    assert got.imap_host == "outlook.office365.com"


def test_a_provider_that_takes_no_password_says_so():
    """Offering a password box to an account that will refuse every password is
    the worst possible first experience. Say it up front instead."""
    fetch = web({"https://autoconfig.thunderbird.net/v1.1/outlook.com": OAUTH_ONLY_XML})
    got = A.discover("s@connect.ust.hk", fetch=fetch,
                     mx=lambda d: ["x.mail.protection.outlook.com"])
    assert got.oauth_only is True


def test_imap_is_preferred_over_pop_and_passwords_over_oauth_only():
    got = A.discover("me@gmail.com", fetch=web(
        {"https://autoconfig.thunderbird.net/v1.1/gmail.com": GMAIL_XML}), mx=lambda d: [])
    assert got.imap_host == "imap.gmail.com" and got.oauth_only is False


def test_username_placeholders_are_filled_in():
    got = A.discover("danny@example.org", fetch=web(
        {"https://autoconfig.thunderbird.net/v1.1/example.org": LOCALPART_XML}), mx=lambda d: [])
    assert got.imap_username == "danny"
    assert got.smtp_username == "danny@example.org"


def test_with_nothing_published_the_guess_is_labelled_a_guess():
    got = A.discover("me@tiny.example", fetch=web({}), mx=lambda d: [])
    assert got.source == "guess"
    assert got.imap_host == "imap.tiny.example"


def test_an_mx_on_the_same_domain_is_not_asked_twice():
    fetch = web({})
    A.discover("me@self.example", fetch=fetch, mx=lambda d: ["mx1.self.example"])
    assert sum("thunderbird.net/v1.1/self.example" in u for u in fetch.asked) == 1


def test_garbage_is_not_an_address():
    assert A.discover("not an address", fetch=web({}), mx=lambda d: []) is None


def test_a_malformed_file_is_skipped_not_fatal():
    fetch = web({"https://autoconfig.example.org/mail/config-v1.1.xml?emailaddress=me%40example.org":
                 "<clientConfig><broken",
                 "https://autoconfig.thunderbird.net/v1.1/example.org": GMAIL_XML})
    assert A.discover("me@example.org", fetch=fetch, mx=lambda d: []).source == "ispdb"


def test_second_level_registries_keep_three_labels():
    assert A._base_domain("mx.mail.example.co.uk") == "example.co.uk"
    assert A._base_domain("aspmx.l.google.com") == "google.com"
