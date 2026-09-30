"""Génération d'un flux iCalendar (RFC 5545) pour abonnement Google / Apple / Outlook."""

from datetime import datetime, timedelta, timezone

from .models import ARMES, Competition


def _echapper(texte: str) -> str:
    # Caractères de contrôle écartés (un « \r » glissé dans ?nom= ne doit pas créer de nouvelle ligne iCalendar)
    texte = "".join(c for c in texte if c == "\n" or c >= " ").replace("\x7f", "")
    return texte.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _plier(ligne: str) -> str:
    """Lignes de 75 octets max, continuation par un espace."""
    brut = ligne.encode("utf-8")
    if len(brut) <= 75:
        return ligne
    morceaux, courant = [], b""
    for car in ligne:
        b = car.encode("utf-8")
        if len(courant) + len(b) > (75 if not morceaux else 74):
            morceaux.append(courant.decode("utf-8"))
            courant = b""
        courant += b
    morceaux.append(courant.decode("utf-8"))
    return "\r\n ".join(morceaux)


def generer(competitions: list[Competition], nom: str, domaine: str) -> str:
    maintenant = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lignes = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//{domaine}//calendrier-ffe//FR",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_echapper(nom)}",
        "X-WR-TIMEZONE:Europe/Paris",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for c in competitions:
        armes = ", ".join(ARMES.get(a, a) for a in c.armes)
        resume = f"{c.titre} – {armes}" if armes else c.titre
        if c.categories_libelle:
            resume += f" ({c.categories_libelle})"
        description = [f"Lieu : {c.lieu}", f"Catégories : {c.categories_libelle or ', '.join(c.categories)}"]
        if c.horaire:
            description.append(f"Horaire : {c.horaire}")
        if c.note_url:
            description.append(f"Note d'organisation : {c.note_url}")
        if c.url:
            description.append(f"Plus d'infos : {c.url}")
        description.append("Sources : " + ", ".join(s.upper() for s in c.sources))
        lignes += [
            "BEGIN:VEVENT",
            f"UID:{c.id}@{domaine}",
            f"DTSTAMP:{maintenant}",
            f"DTSTART;VALUE=DATE:{c.date_debut:%Y%m%d}",
            f"DTEND;VALUE=DATE:{(c.date_fin + timedelta(days=1)):%Y%m%d}",
            f"SUMMARY:{_echapper(resume)}",
            f"LOCATION:{_echapper(c.lieu)}",
            f"DESCRIPTION:{_echapper(chr(10).join(description))}",
            "TRANSP:TRANSPARENT",
        ]
        if c.url:
            lignes.append(f"URL:{c.url}")
        lignes.append("END:VEVENT")
    lignes.append("END:VCALENDAR")
    return "\r\n".join(_plier(l) for l in lignes) + "\r\n"
