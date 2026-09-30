"""Lecture du calendrier public de la FFE (https://www.ffescrime.fr/calendrier/).

Pas d'API officielle : le site est rendu côté serveur, on lit le HTML.
Filtres qui fonctionnent côté FFE : arme[XXX], categories, niveaux, date / final-date
(jj/mm/aaaa, chevauchement), lieu_ville, individuelle_equipe[1|2].
Filtres SANS effet côté FFE : regions, departements, sexe -> on filtre nous-mêmes.
"""

import base64
import binascii
import re
from dataclasses import dataclass, field
from datetime import date

import httpx
from selectolax.parser import HTMLParser, Node

from .securite import lien_web
from .text import categories as extraire_categories
from .text import espaces, plage_dates_ffe

LETTRE_ARME = {"F": "FLE", "E": "EPE", "S": "SAB", "L": "LAS", "A": "ART"}

# L'identifiant FFE est du base64 qui peut contenir '/', '+' et '=' : on le rend sûr pour une URL,
# de façon réversible.
_VERS_ID = str.maketrans({"/": "_", "+": "-", "=": "~"})
_DEPUIS_ID = str.maketrans({"_": "/", "-": "+", "~": "="})
PREFIXE_ID = "ffe-"


# Le jeton décodé ressemble à "2-separator-5-separator-1-separator-101026-separator-111026-separator-ETAMPES" :
# type, échelon, numéro, date de début, date de fin, ville. Type et échelon valent "-" quand ils ne sont pas renseignés.
TYPES = {"1": "tournoi", "2": "epreuve", "3": "championnat"}
ECHELONS = {"2": "departemental", "3": "regional", "4": "zone", "5": "national", "6": "international"}


def type_et_echelon(jeton: str) -> tuple[str | None, str | None]:
    try:
        brut = base64.b64decode(jeton + "=" * (-len(jeton) % 4)).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return None, None
    champs = brut.split("-separator-")
    if len(champs) < 2:
        return None, None
    return TYPES.get(champs[0]), ECHELONS.get(champs[1])


def id_depuis_jeton(jeton: str) -> str:
    return PREFIXE_ID + jeton.translate(_VERS_ID)


def jeton_depuis_id(id_: str) -> str:
    if not id_.startswith(PREFIXE_ID):
        raise ValueError("Identifiant FFE invalide")
    jeton = id_[len(PREFIXE_ID):].translate(_DEPUIS_ID)
    if not re.fullmatch(r"[A-Za-z0-9+/]{4,512}={0,2}", jeton):
        raise ValueError("Identifiant FFE invalide")
    return jeton


@dataclass
class LigneFFE:
    id: str
    jeton: str
    titre: str
    lieu: str
    date_debut: date
    date_fin: date
    armes: list[str]
    categories: list[str]
    categories_libelle: str
    type: str | None = None
    echelon: str | None = None


@dataclass
class FicheFFE:
    titre: str
    lieu: str
    date_debut: date | None
    date_fin: date | None
    armes: list[str]
    categories: list[str]
    categories_libelle: str
    note_organisation: str | None
    site_web: str | None


@dataclass
class Filtres:
    armes: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    niveau: str | None = None
    date_debut: date | None = None
    date_fin: date | None = None
    ville: str | None = None
    equipe: str | None = None  # "individuel" | "equipe"

    def parametres_ffe(self) -> list[tuple[str, str]]:
        """Paramètres GET transmis à la FFE (ordre stable pour servir de clé de cache)."""
        p: list[tuple[str, str]] = [("lieu_ville", self.ville or "")]
        for arme in sorted(set(self.armes)):
            p.append((f"arme[{arme}]", "on"))
        # La FFE n'accepte qu'une catégorie : au-delà, on filtre nous-mêmes.
        p.append(("categories", self.categories[0] if len(self.categories) == 1 else ""))
        p.append(("date", self.date_debut.strftime("%d/%m/%Y") if self.date_debut else ""))
        p.append(("final-date", self.date_fin.strftime("%d/%m/%Y") if self.date_fin else ""))
        p.append(("niveaux", self.niveau or ""))
        if self.equipe == "individuel":
            p.append(("individuelle_equipe[1]", "on"))
        elif self.equipe == "equipe":
            p.append(("individuelle_equipe[2]", "on"))
        return p


def _texte(noeud: Node | None) -> str:
    return espaces(noeud.text(deep=True)) if noeud else ""


def _armes(noeud: Node | None) -> list[str]:
    if noeud is None:
        return []
    armes = []
    for span in noeud.css(".discipline__content span"):
        code = LETTRE_ARME.get(span.text(strip=True).upper())
        if code and code not in armes:
            armes.append(code)
    return armes


def analyser_liste(html: str) -> list[LigneFFE]:
    arbre = HTMLParser(html)
    lignes: list[LigneFFE] = []
    for rangee in arbre.css(".section__table-rows .section__table-row"):
        cellules = rangee.css("ul > li")
        if len(cellules) < 5:
            continue
        lien = cellules[0].css_first("a")
        href = lien.attributes.get("href", "") if lien else ""
        if "/competition/" not in href:
            continue
        jeton = href.split("/competition/", 1)[1].strip("/")
        try:
            debut, fin = plage_dates_ffe(_texte(cellules[2]))
        except ValueError:
            continue
        libelle_cat = _texte(cellules[4])
        type_, echelon = type_et_echelon(jeton)
        lignes.append(
            LigneFFE(
                id=id_depuis_jeton(jeton),
                jeton=jeton,
                titre=_texte(cellules[0]),
                lieu=_texte(cellules[1]),
                date_debut=debut,
                date_fin=fin,
                armes=_armes(cellules[3]),
                categories=extraire_categories(libelle_cat),
                categories_libelle=libelle_cat,
                type=type_,
                echelon=echelon,
            )
        )
    return lignes


def analyser_fiche(html: str) -> FicheFFE:
    arbre = HTMLParser(html)
    principal = arbre.css_first(".section-single-event") or arbre.body
    debut = fin = None
    lieu = libelle_cat = ""
    # Chaque bloc est repéré par son icône : calendrier = dates, épingle = lieu, sans icône = catégories.
    for bloc in principal.css(".section__actions .btn-event"):
        icone = bloc.css_first("img")
        src = icone.attributes.get("src", "") if icone else ""
        texte = _texte(bloc)
        if "calendar" in src:
            try:
                debut, fin = plage_dates_ffe(texte)
            except ValueError:
                pass
        elif "pin" in src:
            lieu = texte
        else:
            libelle_cat = texte
    note = site = None
    for lien in principal.css(".section__actions-links a"):
        libelle = _texte(lien).lower()
        href = lien_web(lien.attributes.get("href"))
        if not href:
            continue
        if "note" in libelle:
            note = href
        elif "site" in libelle:
            site = href
    return FicheFFE(
        titre=_texte(principal.css_first(".section__head h2")),
        lieu=lieu,
        date_debut=debut,
        date_fin=fin,
        armes=_armes(principal.css_first(".section__disciplines")),
        categories=extraire_categories(libelle_cat),
        categories_libelle=libelle_cat,
        note_organisation=note,
        site_web=site,
    )


def analyser_referentiel(html: str) -> dict[str, list[dict[str, str]]]:
    """Valeurs possibles des filtres, lues dans le formulaire de recherche de la FFE."""
    arbre = HTMLParser(html)
    ref: dict[str, list[dict[str, str]]] = {}
    for nom, cle in (("categories", "categories"), ("niveaux", "niveaux")):
        select = arbre.css_first(f'select[name="{nom}"]')
        if select:
            ref[cle] = [
                {"code": o.attributes["value"], "libelle": o.text(strip=True)}
                for o in select.css("option")
                if o.attributes.get("value")
            ]
    armes = []
    for entree in arbre.css('input[type="checkbox"][name^="arme["]'):
        code = entree.attributes["name"][5:-1]
        label = arbre.css_first(f'label[for="{entree.attributes.get("id", "")}"]')
        armes.append({"code": code, "libelle": label.text(strip=True) if label else code})
    ref["armes"] = armes
    return ref


class ClientFFE:
    def __init__(self, http: httpx.AsyncClient, base_url: str):
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def _get(self, chemin: str, params=None) -> str:
        r = await self.http.get(f"{self.base_url}{chemin}", params=params)
        r.raise_for_status()
        return r.text

    async def liste(self, filtres: Filtres) -> list[LigneFFE]:
        return analyser_liste(await self._get("/calendrier/", filtres.parametres_ffe()))

    async def fiche(self, id_: str) -> FicheFFE:
        return analyser_fiche(await self._get(f"/competition/{jeton_depuis_id(id_)}"))

    async def referentiel(self) -> dict[str, list[dict[str, str]]]:
        return analyser_referentiel(await self._get("/calendrier/"))

    def url_fiche(self, id_: str) -> str:
        return f"{self.base_url}/competition/{jeton_depuis_id(id_)}"
