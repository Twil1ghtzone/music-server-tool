"""Gemeinsame Fehlerarten.

Der Unterschied, auf den es im Worker ankommt: manche Fehler gehen beim
naechsten Versuch weg (Netz weg, Dienst startet gerade), andere nie - und die
noch dreimal zu wiederholen erzeugt nur Rauschen im Log und verbrennt die
Versuche, bevor die Ursache ueberhaupt behoben sein kann.
"""
from __future__ import annotations


class PermanentError(RuntimeError):
    """Wiederholen aendert nichts - es fehlt eine Voraussetzung.

    Der Worker markiert solche Jobs sofort als fehlgeschlagen, statt sie
    dreimal in derselben Sekunde durchlaufen zu lassen. Ist die Ursache
    behoben, startet man den Job im Dashboard neu.
    """
