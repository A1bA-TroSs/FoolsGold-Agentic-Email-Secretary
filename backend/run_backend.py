"""Entry point for the frozen (PyInstaller) backend used by packaged builds.

`python -m app.main` is the dev path; a frozen binary needs a plain script.
"""
from app.main import run

if __name__ == "__main__":
    run()
