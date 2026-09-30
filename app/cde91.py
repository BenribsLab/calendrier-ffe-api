"""Calendrier départemental du CDE 91 (Essonne).

Le PDF change de nom à chaque version mais est toujours lié depuis la même page du site (Joomla).
Le PDF est un tableau : date | type | lieu | arme(s) | catégories | horaire, avec des lignes de mois.
"""

import io
import logging
import re
from datetime import date
from urllib.parse import urljoin

import pdfplumber
from selectolax.parser import HTMLParser

from .calendriers import Cible, Document, Evenement, dedoublonner, saison_en_cours, slug
from .text import categories as extraire_categories
from .text import espaces, mois, sans_accents

log = logging.getLogger(__name__)

NOM = "cde91"
LIBELLE = "Calendrier CDE 91"
VERSION_ANALYSE = 4  # à incrémenter à chaque changement de l'analyse

# Remplacement manuel : un seul calendrier, qui remplace tous les PDF de la page du CDE.
CIBLES = [Cible("cde91", "Calendrier CDE 91", lambda document: True)]

ARMES_TEXTE = {"FLEURET": "FLE", "EPEE": "EPE", "SABRE": "SAB", "LASER": "LAS", "ARTISTIQUE": "ART"}


def liens_pdf(html: str, page_url: str) -> list[Document]:
    """Liens PDF du contenu de la page (hors menus)."""
    arbre = HTMLParser(html)
    zone = arbre.css_first('[itemprop="articleBody"]') or arbre.css_first(".item-page") or arbre.body
    liens = []
    for a in zone.css("a[href]"):
        href = a.attributes["href"] or ""
        if href.lower().split("?")[0].endswith(".pdf"):
            url = urljoin(page_url, href)
            if url not in (d.url for d in liens):
                liens.append(Document(url, a.text(strip=True)))
    return liens


def _saison(texte: str) -> tuple[int, int] | None:
    m = re.search(r"SAISON\s*(\d{4})\s*[-/]\s*(\d{4})", texte, re.IGNORECASE)
    return (int(m.group(1)), int(m.group(2))) if m else None


def armes(texte: str) -> list[str]:
    t = sans_accents(texte).upper()
    return [code for mot, code in ARMES_TEXTE.items() if mot in t]


# Abréviations du calendrier du CDE 91 -> libellé affiché
_ABREVIATIONS = [
    (re.compile(r"^CR\b\s*(.*)$", re.IGNORECASE), "Challenge de Référence"),
    (re.compile(r"^CF\s+D[EÉ]PARTEMENTAL(?:E)?\b\s*(.*)$", re.IGNORECASE), "Challenge de France – épreuve départementale"),
    (re.compile(r"^CF\b\s*(.*)$", re.IGNORECASE), "Challenge de France"),
    (re.compile(r"^EDJ\b\s*(.*)$", re.IGNORECASE), "Entraînement des Jeunes"),
]


def titre_complet(titre: str) -> str:
    """'CR 1' -> 'Challenge de Référence 1' ; 'CF DEPARTEMENTAL' -> 'Challenge de France – épreuve départementale' ;
    'EDJ 1' -> 'Entraînement des Jeunes 1'."""
    for motif, libelle in _ABREVIATIONS:
        m = motif.match(titre.strip())
        if m:
            return espaces(f"{libelle} {m.group(1)}")
    return titre


def analyser_pdf(contenu: bytes, document: Document) -> list[Evenement]:
    pdf_url = document.url
    evenements: list[Evenement] = []
    with pdfplumber.open(io.BytesIO(contenu)) as pdf:
        texte_complet = "\n".join(p.extract_text() or "" for p in pdf.pages)
        saison = _saison(texte_complet) or saison_en_cours()
        mois_courant: int | None = None
        for page in pdf.pages:
            for table in page.extract_tables():
                for ligne in table:
                    cellules = [espaces(c or "") for c in ligne]
                    if not any(cellules):
                        continue
                    premiere = cellules[0]
                    # Ligne de mois : "SEPTEMBRE" seul
                    if mois(premiere) and not any(cellules[1:]):
                        mois_courant = mois(premiere)
                        continue
                    if premiere.lower().startswith("vacances"):
                        continue
                    jours = [int(j) for j in re.findall(r"\b(\d{1,2})\b", premiere)]
                    if not jours or mois_courant is None or len(cellules) < 4:
                        continue
                    annee = saison[0] if mois_courant >= 8 else saison[1]
                    try:
                        debut = date(annee, mois_courant, jours[0])
                        fin = date(annee, mois_courant, jours[-1])
                    except ValueError:
                        log.warning("Date invalide dans le PDF CDE : %r", premiere)
                        continue
                    titre, lieu, armes_txt = cellules[1], cellules[2], cellules[3]
                    libelle_cat = cellules[4] if len(cellules) > 4 else ""
                    horaire = cellules[5] if len(cellules) > 5 and cellules[5] else None
                    codes = armes(armes_txt)
                    evenements.append(
                        Evenement(
                            id=f"{NOM}-{debut.isoformat()}-{slug(titre)}-{slug(lieu)}-{'-'.join(a.lower() for a in codes)}",
                            titre=titre_complet(titre),
                            lieu=lieu,
                            date_debut=debut,
                            date_fin=max(debut, fin),
                            armes=codes,
                            categories=extraire_categories(libelle_cat),
                            categories_libelle=libelle_cat,
                            horaire=horaire,
                            pdf_url=pdf_url,
                        )
                    )
    return dedoublonner(evenements)
