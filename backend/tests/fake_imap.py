"""A real IMAP server, in-process, speaking the protocol on a real socket.

The same argument as `fake_smtp.py`, and it applies harder here: the thing
worth proving is that a reply leaves this app as bytes an IMAP server accepts,
**with its threading headers intact**, via an `APPEND` literal -- and a mock of
`imaplib` can only confirm that we called `imaplib` the way we called it.

Implements the handful of commands the transports use: CAPABILITY, LOGIN,
LIST, SELECT, UID SEARCH, APPEND (with a `{n}` literal and the `+`
continuation it requires), NOOP, LOGOUT. Plain TCP only -- TLS against a
self-signed certificate would fail the client's certificate check, which is the
correct behaviour and not something to disable for a test.
"""
from __future__ import annotations

import re
import socket
import threading

DEFAULT_MAILBOXES = [
    ("\\HasNoChildren", "INBOX"),
    ("\\HasNoChildren \\Sent", "Sent Messages"),
    ("\\HasNoChildren \\Drafts", "Drafts"),
]


class FakeIMAP(threading.Thread):
    daemon = True

    def __init__(self, *, username: str = "danny@example.edu", password: str = "app-password",
                 mailboxes=None, starttls: bool = False, persistent: bool = False):
        super().__init__()
        self.username, self.password = username, password
        self.mailboxes = list(mailboxes or DEFAULT_MAILBOXES)
        self.starttls = starttls
        # Serve connection after connection -- the E2E test logs in once to
        # check the settings and again to save the draft.
        self.persistent = persistent
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.port: int = self._sock.getsockname()[1]
        self.ready = threading.Event()
        self.logins: list[tuple[str, str]] = []
        self.appended: list[dict] = []
        self.selected: list[str] = []          # {mailbox, flags, message}
        self.commands: list[str] = []
        self._stop = False

    # ------------------------------------------------------------ plumbing

    def run(self) -> None:
        self.ready.set()
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            try:
                self._serve(conn)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
            if not self.persistent:
                return

    def stop(self) -> None:
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass

    def _serve(self, conn: socket.socket) -> None:
        stream = conn.makefile("rwb")

        def say(line: str) -> None:
            stream.write(line.encode("utf-8") + b"\r\n")
            stream.flush()

        caps = "IMAP4rev1 LITERAL+ SPECIAL-USE" + (" STARTTLS" if self.starttls else "")
        say(f"* OK [CAPABILITY {caps}] fake IMAP ready")
        authed = False
        while True:
            raw = stream.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            self.commands.append(line)
            parts = line.split(" ", 2)
            if len(parts) < 2:
                continue
            tag, verb = parts[0], parts[1].upper()
            rest = parts[2] if len(parts) > 2 else ""

            if verb == "CAPABILITY":
                say(f"* CAPABILITY {caps}")
                say(f"{tag} OK done")
            elif verb == "NOOP":
                say(f"{tag} OK done")
            elif verb == "LOGIN":
                user, pw = _atoms(rest)
                self.logins.append((user, pw))
                if (user, pw) == (self.username, self.password):
                    authed = True
                    say(f"{tag} OK [CAPABILITY {caps}] logged in")
                else:
                    say(f"{tag} NO [AUTHENTICATIONFAILED] invalid credentials")
            elif not authed and verb not in ("LOGOUT",):
                say(f"{tag} BAD not authenticated")
            elif verb == "LIST":
                for flags, name in self.mailboxes:
                    say(f'* LIST ({flags}) "/" "{name}"')
                say(f"{tag} OK done")
            elif verb in ("SELECT", "EXAMINE"):
                # As strict as a real server: a name with a space must be
                # quoted, or it is two arguments and the command is malformed.
                if not re.match(r'^("[^"]*"|\S+)$', rest.strip()):
                    say(f"{tag} BAD malformed {verb}")
                    continue
                self.selected.append(rest.strip().strip('"'))
                say("* 0 EXISTS")
                say(f"{tag} OK [READ-WRITE] selected")
            elif verb == "UID":
                say("* SEARCH")
                say(f"{tag} OK done")
            elif verb == "APPEND":
                self._append(tag, rest, stream, say)
            elif verb == "LOGOUT":
                say("* BYE")
                say(f"{tag} OK bye")
                return
            else:
                say(f"{tag} BAD unsupported {verb}")

    def _append(self, tag, rest, stream, say) -> None:
        """`APPEND "Drafts" (\\Draft) "date" {123}` then the literal."""
        match = re.match(r'^(?P<box>"[^"]*"|\S+)\s+(?:\((?P<flags>[^)]*)\)\s+)?'
                         r'(?:"(?P<date>[^"]*)"\s+)?\{(?P<n>\d+)(?P<plus>\+?)\}$', rest)
        if not match:
            say(f"{tag} BAD malformed APPEND")
            return
        box = match.group("box").strip('"')
        if match.group("plus") != "+":
            say("+ ready for literal")
        data = stream.read(int(match.group("n")))
        stream.readline()                        # the CRLF after the literal
        if box not in {name for _, name in self.mailboxes}:
            say(f"{tag} NO [TRYCREATE] no such mailbox")
            return
        self.appended.append({"mailbox": box, "flags": match.group("flags") or "",
                              "message": data})
        say(f"{tag} OK [APPENDUID 1 {len(self.appended)}] appended")


def _atoms(text: str) -> tuple[str, str]:
    """Two IMAP atoms or quoted strings: `user pass` or `"user" "pass"`."""
    tokens = re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', text)
    vals = [a.replace('\\"', '"').replace("\\\\", "\\") if a else b for a, b in tokens]
    return (vals + ["", ""])[0], (vals + ["", ""])[1]
