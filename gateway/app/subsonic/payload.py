"""Subsonic-Antworten kodieren.

Trick, der viel Arbeit spart: von Navidrome fordern wir IMMER f=json an, auch
wenn der Client XML will. Manipuliert wird also nur ein dict; erst beim
Ausliefern wird in das Format serialisiert, das der Client verlangt hat.
XML-Baeume zu patchen waere deutlich fehleranfaelliger.

Serialisierungsregeln (entsprechen dem offiziellen Subsonic-Mapping):
  Skalar          -> Attribut
  dict            -> Kindelement
  Liste von dicts -> wiederholte Kindelemente
  Liste von Skalaren -> wiederholte Elemente mit Textinhalt
  Schluessel "value" -> Textinhalt des Elements
"""
from __future__ import annotations

import json
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from fastapi import Response

API_VERSION = "1.16.1"

_XML_HEADER = '<?xml version="1.0" encoding="UTF-8"?>'
_NS = "http://subsonic.org/restapi"

JSON_CT = "application/json; charset=utf-8"
XML_CT = "text/xml; charset=utf-8"
JSONP_CT = "application/javascript; charset=utf-8"

# Subsonic-Fehlercodes
E_GENERIC = 0
E_MISSING_PARAM = 10
E_AUTH = 40
E_NOT_FOUND = 70


def wanted_format(params) -> str:
    fmt = (params.get("f") or "xml").lower()
    return fmt if fmt in ("xml", "json", "jsonp") else "xml"


def envelope(payload: dict[str, Any] | None = None, *, version: str = API_VERSION) -> dict[str, Any]:
    body: dict[str, Any] = {"status": "ok", "version": version, "type": "music-gateway"}
    if payload:
        body.update(payload)
    return {"subsonic-response": body}


def error_envelope(code: int, message: str, *, version: str = API_VERSION) -> dict[str, Any]:
    return {
        "subsonic-response": {
            "status": "failed",
            "version": version,
            "type": "music-gateway",
            "error": {"code": code, "message": message},
        }
    }


# ----------------------------------------------------------------- XML
def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _element(name: str, node: Any, out: list[str]) -> None:
    if isinstance(node, dict):
        attrs: list[str] = []
        children: list[tuple[str, Any]] = []
        text: str | None = None
        for key, value in node.items():
            if value is None:
                continue
            if key == "value" and not isinstance(value, (dict, list)):
                text = _scalar(value)
            elif isinstance(value, (dict, list)):
                children.append((key, value))
            else:
                attrs.append(f"{key}={quoteattr(_scalar(value))}")
        head = f"<{name}" + (" " + " ".join(attrs) if attrs else "")
        if not children and text is None:
            out.append(head + "/>")
            return
        out.append(head + ">")
        if text is not None:
            out.append(escape(text))
        for key, value in children:
            _element(key, value, out)
        out.append(f"</{name}>")
    elif isinstance(node, list):
        for item in node:
            _element(name, item, out)
    else:
        out.append(f"<{name}>{escape(_scalar(node))}</{name}>")


def to_xml(document: dict[str, Any]) -> bytes:
    body = document.get("subsonic-response") or {}
    attrs = {
        "xmlns": _NS,
        "status": body.get("status", "ok"),
        "version": body.get("version", API_VERSION),
    }
    for optional in ("type", "serverVersion", "openSubsonic"):
        if optional in body:
            attrs[optional] = _scalar(body[optional])

    out: list[str] = [_XML_HEADER]
    out.append("<subsonic-response " + " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items()) + ">")
    for key, value in body.items():
        if key in ("status", "version", "type", "serverVersion", "openSubsonic"):
            continue
        if value is None:
            continue
        _element(key, value, out)
    out.append("</subsonic-response>")
    return "".join(out).encode("utf-8")


# --------------------------------------------------------------- Response
def render(document: dict[str, Any], fmt: str, callback: str | None = None,
           status_code: int = 200) -> Response:
    if fmt == "json":
        return Response(
            content=json.dumps(document, ensure_ascii=False, separators=(",", ":")),
            media_type=JSON_CT,
            status_code=status_code,
        )
    if fmt == "jsonp":
        cb = callback or "callback"
        payload = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        return Response(content=f"{cb}({payload});", media_type=JSONP_CT, status_code=status_code)
    return Response(content=to_xml(document), media_type=XML_CT, status_code=status_code)


def error(code: int, message: str, params) -> Response:
    return render(error_envelope(code, message), wanted_format(params), params.get("callback"))


def ok(payload: dict[str, Any] | None, params) -> Response:
    return render(envelope(payload), wanted_format(params), params.get("callback"))


# ------------------------------------------------- Virtueller Subsonic-Song
_STATE_HINT = {
    "queued": "In Warteschlange",
    "downloading": "Wird geladen",
    "importing": "Wird importiert",
    "failed": "Fehlgeschlagen",
}


def virtual_song(row: dict[str, Any], marker: str) -> dict[str, Any]:
    """Baut aus einer virtual_track-Zeile ein Subsonic-<child>-Objekt.

    Alle Felder, die gaengige Clients (Substreamer, Symfonium, DSub) fuer die
    Darstellung und das Anlegen einer Warteschlange brauchen, sind gesetzt -
    fehlende Pflichtfelder fuehren bei manchen Clients zum stillen Ausblenden
    des Eintrags.
    """
    state = row.get("state") or "virtual"
    hint = _STATE_HINT.get(state)
    suffix = f" [{hint}]" if hint else marker
    duration = int(row.get("duration") or 0)
    vid = row["id"]
    return {
        "id": vid,
        "parent": f"{vid}-album",
        "isDir": False,
        "title": (row.get("title") or "") + suffix,
        "album": row.get("album") or "",
        "artist": row.get("artist") or "",
        "albumArtist": row.get("album_artist") or row.get("artist") or "",
        "track": row.get("track_no") or 0,
        "discNumber": row.get("disc_no") or 1,
        "year": row.get("year") or 0,
        "genre": "",
        "coverArt": vid,
        "size": max(duration * 40000, 1),   # Schaetzung: ~320 kbit/s
        "contentType": "audio/mpeg",
        "suffix": "mp3",
        "duration": duration,
        "bitRate": 320,
        "path": f"_gateway/{row.get('artist','')}/{row.get('title','')}.mp3",
        "playCount": 0,
        "created": row.get("created_at") or "1970-01-01T00:00:00.000Z",
        "albumId": f"{vid}-album",
        "artistId": f"{vid}-artist",
        "type": "music",
        "isVideo": False,
        # OpenSubsonic-Felder, von neueren Clients ausgewertet
        "mediaType": "song",
        "sortName": row.get("title") or "",
        "musicBrainzId": "",
        "comment": f"music-gateway: {state}",
    }
