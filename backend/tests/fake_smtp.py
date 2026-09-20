"""A real SMTP server, in-process, for the transport tests.

Deliberately not a mock of `smtplib`. The things worth testing here happen on
the wire -- whether `Bcc` appears in the transmitted bytes, which addresses
reach `RCPT TO`, whether a password is offered to a server that never
advertised encryption -- and a mock of the client can only ever confirm that we
called the client the way we called it.

Small enough to own outright rather than take `aiosmtpd` as a dependency for.
"""
from __future__ import annotations

import base64
import socket
import threading


class FakeSMTP(threading.Thread):
    daemon = True

    def __init__(self, *, auth_ok: bool = True, advertise_starttls: bool = False,
                 refuse: tuple[str, ...] = (), data_reply: str = "250 queued") -> None:
        super().__init__()
        self.auth_ok = auth_ok
        self.advertise_starttls = advertise_starttls
        self.refuse = tuple(a.lower() for a in refuse)
        self.data_reply = data_reply
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port: int = self._sock.getsockname()[1]
        # what the client did
        self.credentials: tuple[str, str] | None = None
        self.mail_from: str = ""
        self.rcpt_to: list[str] = []
        self.payload: bytes = b""
        self.commands: list[str] = []
        self.ready = threading.Event()

    def run(self) -> None:
        self.ready.set()
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn, conn.makefile("rwb") as stream:
            def say(text: str) -> None:
                stream.write(text.encode() + b"\r\n")
                stream.flush()

            say("220 fake ESMTP")
            while True:
                line = stream.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").strip()
                verb = text.split(" ", 1)[0].upper()
                self.commands.append(text)

                if verb in ("EHLO", "HELO"):
                    extensions = ["250-fake", "250-8BITMIME"]
                    if self.advertise_starttls:
                        extensions.append("250-STARTTLS")
                    extensions.append("250 AUTH PLAIN LOGIN")
                    say("\r\n".join(extensions))
                elif verb == "AUTH":
                    self._auth(text, stream, say)
                elif verb == "MAIL":
                    self.mail_from = _angle(text)
                    say("250 ok")
                elif verb == "RCPT":
                    address = _angle(text)
                    if address.lower() in self.refuse:
                        say("550 no such user")
                    else:
                        self.rcpt_to.append(address)
                        say("250 ok")
                elif verb == "DATA":
                    say("354 go ahead")
                    chunks = []
                    while True:
                        chunk = stream.readline()
                        if not chunk or chunk.strip() == b".":
                            break
                        chunks.append(chunk)
                    self.payload = b"".join(chunks)
                    say(self.data_reply)
                elif verb == "RSET":
                    say("250 ok")
                elif verb == "QUIT":
                    say("221 bye")
                    return
                else:
                    say("502 not implemented")

    def _auth(self, text: str, stream, say) -> None:
        parts = text.split()
        mechanism = parts[1].upper() if len(parts) > 1 else ""
        if mechanism == "PLAIN":
            blob = parts[2] if len(parts) > 2 else stream.readline().decode().strip()
            decoded = base64.b64decode(blob).split(b"\x00")
            if len(decoded) == 3:
                self.credentials = (decoded[1].decode(), decoded[2].decode())
        elif mechanism == "LOGIN":
            say("334 " + base64.b64encode(b"Username:").decode())
            user = base64.b64decode(stream.readline().strip()).decode()
            say("334 " + base64.b64encode(b"Password:").decode())
            password = base64.b64decode(stream.readline().strip()).decode()
            self.credentials = (user, password)
        say("235 accepted" if self.auth_ok else "535 bad credentials")

    def stop(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def _angle(text: str) -> str:
    start, end = text.find("<"), text.rfind(">")
    if start >= 0 and end > start:
        return text[start + 1:end]
    return text.split(":", 1)[-1].strip()
