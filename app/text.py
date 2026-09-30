"""Petits utilitaires de normalisation de texte (dates françaises, villes, catégories)."""

import re
import unicodedata
from datetime import date

from .models import CATEGORIES

MOIS = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
}


def sans_accents(texte: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texte) if unicodedata.category(c) != "Mn")


def espaces(texte: str) -> str:
    return re.sub(r"\s+", " ", texte).strip()


def nom_developpe(ville: str) -> str:
    """'SAVIGNY S/ ORGE', 'Savigny/Orge' -> 'SAVIGNY SUR ORGE' ; 'St Maur' -> 'SAINT MAUR' (sans accents, en majuscules)."""
    v = sans_accents(ville).upper()
    v = re.sub(r"\bS/\s*", "SUR ", v)
    v = re.sub(r"(?<=[A-Z])\s*/\s*(?=[A-Z])", " SUR ", v)
    v = re.sub(r"\bST\b\.?", "SAINT", v)
    v = re.sub(r"\bSTE\b\.?", "SAINTE", v)
    return espaces(v)


def cle_ville(ville: str) -> str:
    """Clé de comparaison : 'SAVIGNY S/ ORGE', 'Savigny/Orge' et 'Savigny-sur-Orge' donnent 'SAVIGNYSURORGE'."""
    return re.sub(r"[^A-Z0-9]", "", nom_developpe(ville))


def mois(nom: str) -> int | None:
    return MOIS.get(sans_accents(nom).lower())


_PLAGE_FFE = re.compile(
    r"Du\s+(\d{1,2})(?:\s+([^\W\d_]+))?(?:\s+(\d{4}))?\s+au\s+(\d{1,2})\s+([^\W\d_]+)\s+(\d{4})",
    re.IGNORECASE,
)


def plage_dates_ffe(texte: str) -> tuple[date, date]:
    """'Du 04 au 04 octobre 2026', 'Du 22 septembre au 03 octobre 2026',
    'Du 28 décembre 2026 au 02 janvier 2027'."""
    m = _PLAGE_FFE.search(espaces(texte))
    if not m:
        raise ValueError(f"Plage de dates non reconnue : {texte!r}")
    j1, m1, a1, j2, m2, a2 = m.groups()
    mois_fin = mois(m2)
    if mois_fin is None:
        raise ValueError(f"Mois inconnu : {m2!r}")
    mois_debut = mois(m1) if m1 else mois_fin
    if mois_debut is None:
        raise ValueError(f"Mois inconnu : {m1!r}")
    annee_fin = int(a2)
    annee_debut = int(a1) if a1 else (annee_fin - 1 if mois_debut > mois_fin else annee_fin)
    return date(annee_debut, mois_debut, int(j1)), date(annee_fin, mois_fin, int(j2))


def categories(texte: str) -> list[str]:
    """Extrait les catégories d'un libellé libre.

    'M9, M11, M13' -> [M9, M11, M13] ; 'M15 et plus' -> M15..V4 ;
    'M11 à Vétérans' -> M11..V4 ; 'Vétérans' -> V1..V4.
    """
    t = sans_accents(texte).upper()
    t = re.sub(r"\bSENIORS?\b", "SENIOR", t)
    jetons = re.findall(r"\bM\d{1,2}\b|\bSENIOR\b|\bV[1-4]\b|\bVETERANS?\b", t)
    resultat: list[str] = []

    def ajouter(cats):
        for c in cats:
            if c in CATEGORIES and c not in resultat:
                resultat.append(c)

    def index(jeton, fin=False):
        if jeton.startswith("VETERAN"):
            return CATEGORIES.index("V4" if fin else "V1")
        return CATEGORIES.index(jeton) if jeton in CATEGORIES else None

    plage = re.search(r"(\bM\d{1,2}\b|\bSENIOR\b|\bV[1-4]\b)\s*(?:A|AU|->|-)\s*(\bM\d{1,2}\b|\bSENIOR\b|\bV[1-4]\b|\bVETERANS?\b)", t)
    if plage:
        i, j = index(plage.group(1)), index(plage.group(2), fin=True)
        if i is not None and j is not None and i <= j:
            ajouter(CATEGORIES[i : j + 1])
    for jeton in jetons:
        if jeton.startswith("VETERAN"):
            ajouter(["V1", "V2", "V3", "V4"])
        else:
            ajouter([jeton])
    if re.search(r"ET PLUS|ET \+|\+$", t) and jetons:
        i = index(jetons[-1])
        if i is not None:
            ajouter(CATEGORIES[i:])
    return sorted(resultat, key=CATEGORIES.index)
