"""Fools Gold backend.

Binds to 127.0.0.1 only. Nothing about this process is reachable from the
network -- it exists to give the Electron window a local API and to keep the
Microsoft and LLM credentials out of the renderer.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import db, pipeline
from .config import APP_NAME, BACKEND_HOST, BACKEND_PORT, VERSION
from .graph import auth as graph_auth
from .routers import auth as auth_router
from .routers import calendar as calendar_router
from .routers import config as config_router
from .routers import mail as mail_router
from .routers import ranking as ranking_router
from .sources.registry import get_source

POLL_SECONDS = 300  # background refresh while the window is open


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    task = asyncio.create_task(_poller())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _poller() -> None:
    """Periodic sync. Failures must not take down the app someone is reading
    mail in -- but they must not be invisible either.

    They were. `except Exception: pass` meant a mailbox frozen for five days
    looked exactly like a quiet week, and there was nowhere to look it up. Every
    attempt now lands in `sync_events` with its outcome, so "nothing arrived"
    and "this has been failing since Monday" are different answers to the same
    question."""
    while True:
        await asyncio.sleep(POLL_SECONDS)
        started = db.now_iso()
        try:
            source = get_source()
            status = source.status()
            if not status.ready:
                db.record_sync("poller", started, ok=False,
                               error=status.detail or "the mail source is not ready")
                continue
            result = await source.sync()
            db.record_sync("poller", started, ok=True,
                           scanned=result.get("scanned", 0),
                           fetched=result.get("fetched", 0),
                           written=result.get("written", 0))
            await pipeline.classify_pending(limit=50)
        except Exception as exc:  # noqa: BLE001
            try:
                db.record_sync("poller", started, ok=False, error=f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001
                pass


app = FastAPI(title=APP_NAME, version=VERSION, lifespan=lifespan)

# The renderer is served from this same origin in production; the permissive
# localhost rule exists so `npm run dev` (Vite on :5173) works too.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(mail_router.router)
app.include_router(calendar_router.router)
app.include_router(config_router.router)
app.include_router(ranking_router.router)


@app.get("/api/health")
def health() -> dict:
    """Must always answer 200. The Electron shell polls this to decide the
    backend is up, and the UI reads the source block on every page load, so a
    failure in one source has to degrade into a message rather than a 500 --
    otherwise a fixable permission problem looks like a dead app."""
    try:
        source = get_source()
        source_block = {"name": source.name, "label": source.label, **source.status().as_dict()}
    except Exception as exc:  # noqa: BLE001
        source_block = {
            "name": "unknown",
            "label": "Mail",
            "ready": False,
            "needs_setup": True,
            "needs_auth": False,
            "account": None,
            "detail": f"Could not read the mail source: {exc}",
        }

    try:
        auth_block = graph_auth.status()
    except Exception as exc:  # noqa: BLE001
        auth_block = {"configured": False, "signed_in": False, "account": None, "detail": str(exc)}

    return {
        "status": "ok",
        "app": APP_NAME,
        "version": VERSION,
        # So the desktop shell can tell an orphaned backend of ours from some
        # other program on the port, and end it rather than adopting whatever
        # code it happens to be running. Local-only endpoint on a loopback
        # socket; the pid is not a secret from the machine it is running on.
        "pid": os.getpid(),
        "source": source_block,
        "auth": auth_block,
        "ai": pipeline.ai_status(),
    }


# Serve the built React app when it exists, so the packaged Electron window and
# a plain browser both work from one origin.
_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="ui")


def run() -> None:
    uvicorn.run(app, host=BACKEND_HOST, port=BACKEND_PORT, log_level="info")


if __name__ == "__main__":
    run()
