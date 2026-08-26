"""Outlook sign-in endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .. import db
from ..graph import auth as graph_auth
from ..graph import sync as graph_sync

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str | None = None


@router.get("/status")
def status() -> dict:
    return graph_auth.status()


@router.post("/login")
def login(body: LoginRequest) -> dict:
    """Returns the Microsoft consent URL. The Electron shell opens it in the
    user's real browser so they can verify the login.microsoftonline.com origin
    themselves -- we never render Microsoft's password field inside our window."""
    try:
        url = graph_auth.build_auth_url((body.email or "").strip() or None)
    except graph_auth.NotConfigured as exc:
        raise HTTPException(status_code=428, detail=str(exc)) from exc
    except graph_auth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"auth_url": url}


@router.get("/callback", response_class=HTMLResponse)
async def callback(request: Request) -> HTMLResponse:
    """Microsoft redirects the browser here after consent."""
    try:
        account = graph_auth.redeem_code(dict(request.query_params))
    except graph_auth.AuthError as exc:
        return HTMLResponse(_page("Sign-in failed", str(exc), ok=False), status_code=400)

    try:
        profile = await graph_sync.fetch_profile()
        if profile.get("address"):
            with db.connect() as conn:
                conn.execute("UPDATE oauth_tokens SET account = ? WHERE id = 1", (profile["address"],))
                conn.commit()
            account = profile["address"]
    except Exception:  # noqa: BLE001 - profile is a nicety, not a blocker
        pass

    return HTMLResponse(_page("Signed in", f"Connected as {account}. You can close this tab."))


@router.post("/logout")
def logout() -> dict:
    graph_auth.sign_out()
    return {"signed_in": False}


def _page(title: str, message: str, ok: bool = True) -> str:
    accent = "#C9A227" if ok else "#B4483C"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title></head>
<body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
             background:#FAF6EC;font-family:-apple-system,Segoe UI,sans-serif;color:#3A2E1F">
  <div style="text-align:center;max-width:420px;padding:40px">
    <div style="width:48px;height:48px;border-radius:14px;background:{accent};margin:0 auto 20px"></div>
    <h1 style="font-size:20px;margin:0 0 8px">{title}</h1>
    <p style="color:#6B5B3A;line-height:1.5;margin:0">{message}</p>
  </div>
</body></html>"""
