"""Lokaler Entwicklungsstart ohne Docker.

Legt ein Wegwerf-Datenverzeichnis an, setzt sinnvolle Voreinstellungen und
startet die API auf Port 8099. Navidrome und Deemix duerfen fehlen - das
Dashboard laeuft trotzdem und zeigt sie als offline.

    cd gateway && python tests/devserver.py

Zugang: admin / devpassword123
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = Path(os.environ.get("MST_DEV_DIR") or (Path(tempfile.gettempdir()) / "mst-dev"))
for name in ("music", "staging", "quarantine", "data", "cache"):
    (BASE / name).mkdir(parents=True, exist_ok=True)

os.environ.setdefault("GATEWAY_ROLE", "api")
os.environ.setdefault("LOG_LEVEL", "info")
os.environ.setdefault("DB_PATH", str(BASE / "data" / "gateway.db"))
os.environ.setdefault("CACHE_DIR", str(BASE / "cache"))
os.environ.setdefault("MUSIC_DIR", str(BASE / "music"))
os.environ.setdefault("STAGING_DIR", str(BASE / "staging"))
os.environ.setdefault("QUARANTINE_DIR", str(BASE / "quarantine"))
os.environ.setdefault("NAVIDROME_URL", "http://127.0.0.1:4533")
# Bewusst kein Passwort: das ist der Auslieferungszustand. Der Zugang wird
# im Dashboard unter Diagnose hinterlegt.
os.environ.setdefault("DEEMIX_URL", "http://127.0.0.1:6595")
os.environ.setdefault("GATEWAY_ADMIN_USER", "admin")
os.environ.setdefault("GATEWAY_ADMIN_PASSWORD", "devpassword123")
os.environ.setdefault("GATEWAY_SESSION_SECRET", "dev" * 20)

if __name__ == "__main__":
    import uvicorn

    print(f"Datenverzeichnis: {BASE}")
    print("Dashboard:        http://127.0.0.1:8099   (admin / devpassword123)")
    uvicorn.run("app.main:app", host="127.0.0.1", port=8099, reload=False)
