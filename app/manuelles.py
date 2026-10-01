"""Compétitions ajoutées à la main (administration du club), une par une ou par import CSV.

Enregistrées dans <data_dir>/manuelles.json. Une compétition « liée FFE » sera publiée plus tard sur le site
fédéral : dès qu'elle y apparaît (mêmes dates, même lieu, même arme), la fiche FFE prend le relais et garde les
informations du club (remarque, document, préinscription…). Voir `Service._fusionner_manuelle`.
"""

import csv
import io
import json
import logging
import re
import secrets
from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .models import ARMES, CATEGORIES
from .securite import lien_web
from .text import categories as categories_du_texte
from .text import sans_accents

log = logging.getLogger(__name__)

NOM = "manuel"
LIBELLE = "Ajout manuel"
TAILLE_MAX_CSV = 1024 * 1024
LIGNES_MAX_CSV = 1000

# Colonnes du CSV, dans l'ordre du modèle. Les en-têtes sont reconnus sans tenir compte des accents ni des majuscules.
COLONNES = [
    "nom", "lieu", "date_debut", "date_fin", "armes", "categories", "document_url", "document_nom",
    "preinscription", "delai_inscription_jours", "inscription_sur_place", "liee_ffe", "remarque",
]
SYNONYMES = {
    "titre": "nom", "date": "date_debut", "debut": "date_debut", "fin": "date_fin", "arme": "armes",
    "categorie": "categories", "document": "document_url", "lien_document": "document_url",
    "nom_document": "document_nom", "preinscription_possible": "preinscription",
    "delai": "delai_inscription_jours", "delai_jours": "delai_inscription_jours",
    "duree_avant_hors_delai": "delai_inscription_jours", "sur_place": "inscription_sur_place",
    "lie_ffe": "liee_ffe", "liee_federation": "liee_ffe", "ffe": "liee_ffe", "remarques": "remarque",
}
NOMS_ARMES = {sans_accents(v).upper(): k for k, v in ARMES.items()}
VRAI = {"OUI", "O", "1", "X", "VRAI", "TRUE", "YES", "Y"}
FAUX = {"NON", "N", "0", "FAUX", "FALSE", "NO", ""}


class SaisieManuelle(BaseModel):
    """Ce que le club saisit (formulaire ou ligne de CSV)."""

    titre: str = Field(min_length=1, max_length=200, description="Nom de la compétition")
    lieu: str = Field(min_length=1, max_length=120, description="Commune (sert à la distance et au département)")
    date_debut: date
    date_fin: date | None = Field(None, description="Vide : même jour que le début")
    armes: list[str] = Field(default_factory=list, description="Vide : toutes les armes. Codes : " + ", ".join(ARMES))
    categories: list[str] = Field(min_length=1, description=", ".join(CATEGORIES))
    document_url: str | None = Field(None, max_length=2000, description="Document support (lien http(s))")
    document_nom: str | None = Field(None, max_length=120)
    preinscription: bool = Field(True, description="Préinscription possible sur le site du club")
    delai_inscription_jours: int | None = Field(
        None, ge=1, le=90, description="Hors délai à partir de N jours avant le début (à midi) ; vide : règle habituelle"
    )
    inscription_sur_place: bool = False
    liee_ffe: bool = Field(False, description="Sera publiée sur le site fédéral : la fiche FFE prendra le relais")
    remarque: str | None = Field(None, max_length=1000)

    @field_validator("titre", "lieu")
    @classmethod
    def _texte(cls, v: str) -> str:
        v = re.sub(r"\s+", " ", v).strip()
        if not v:
            raise ValueError("obligatoire")
        return v

    @field_validator("document_nom")
    @classmethod
    def _facultatif(cls, v: str | None) -> str | None:
        return v.strip() or None if v else None

    @field_validator("remarque")
    @classmethod
    def _remarque(cls, v: str | None) -> str | None:
        # « \n » écrit tel quel (CSV fait à la main, ou \r\n, \r) : retour à la ligne
        return re.sub(r"\\r\\n|\\n|\\r|\r\n?", "\n", v).strip() or None if v else None

    @field_validator("document_url")
    @classmethod
    def _lien(cls, v: str | None) -> str | None:
        if not v or not v.strip():
            return None
        lien = lien_web(v)
        if not lien:
            raise ValueError("le document doit être un lien http(s)")
        return lien

    @field_validator("armes")
    @classmethod
    def _armes(cls, v: list[str]) -> list[str]:
        inconnues = [a for a in v if a not in ARMES]
        if inconnues:
            raise ValueError(f"arme(s) inconnue(s) : {', '.join(inconnues)} (codes : {', '.join(ARMES)})")
        return [a for a in ARMES if a in v]

    @field_validator("categories")
    @classmethod
    def _categories(cls, v: list[str]) -> list[str]:
        inconnues = [c for c in v if c not in CATEGORIES]
        if inconnues:
            raise ValueError(f"catégorie(s) inconnue(s) : {', '.join(inconnues)} (codes : {', '.join(CATEGORIES)})")
        return [c for c in CATEGORIES if c in v]

    @model_validator(mode="after")
    def _dates(self):
        if self.date_fin is None:
            self.date_fin = self.date_debut
        if self.date_fin < self.date_debut:
            raise ValueError("la date de fin est antérieure à la date de début")
        return self


class CompetitionManuelle(SaisieManuelle):
    id: str
    cree_le: datetime
    modifie_le: datetime


def libelle_categories(categories: list[str]) -> str:
    noms = {"SENIOR": "Seniors"}
    if categories and all(c in categories for c in ("V1", "V2", "V3", "V4")):
        categories = [c for c in categories if not c.startswith("V")] + ["Vétérans"]
    return ", ".join(noms.get(c, c) for c in categories)


def _nouvel_id(s: SaisieManuelle) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", sans_accents(f"{s.titre} {s.lieu}").lower()).strip("-")[:60].strip("-")
    return f"{NOM}-{s.date_debut.isoformat()}-{base}-{secrets.token_hex(3)}"


class Manuelles:
    def __init__(self, dossier: Path):
        self.dossier = dossier
        self.fichier = dossier / "manuelles.json"
        self.competitions: dict[str, CompetitionManuelle] = self._charger()

    def _charger(self) -> dict[str, CompetitionManuelle]:
        try:
            brut = json.loads(self.fichier.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.exception("Lecture impossible : %s", self.fichier)
            return {}
        resultat = {}
        for item in brut if isinstance(brut, list) else []:
            try:
                c = CompetitionManuelle.model_validate(item)
                resultat[c.id] = c
            except ValidationError:
                log.exception("Compétition manuelle illisible, ignorée : %r", item)
        if any(c.model_dump(mode="json") != item for item, c in zip(brut, resultat.values())):
            self.competitions = resultat
            self._sauver()  # ex. remarque corrigée à la lecture (« \n » devenu un retour à la ligne)
        return resultat

    def _sauver(self) -> None:
        self.dossier.mkdir(parents=True, exist_ok=True)
        tmp = self.fichier.with_suffix(".tmp")
        donnees = [c.model_dump(mode="json") for c in self.liste()]
        tmp.write_text(json.dumps(donnees, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.fichier)

    def liste(self) -> list[CompetitionManuelle]:
        return sorted(self.competitions.values(), key=lambda c: (c.date_debut, c.titre, c.lieu))

    def get(self, id_: str) -> CompetitionManuelle | None:
        return self.competitions.get(id_)

    def ajouter(self, saisies: list[SaisieManuelle]) -> list[CompetitionManuelle]:
        maintenant = datetime.now().astimezone()
        nouvelles = [
            CompetitionManuelle(**s.model_dump(), id=_nouvel_id(s), cree_le=maintenant, modifie_le=maintenant)
            for s in saisies
        ]
        for c in nouvelles:
            self.competitions[c.id] = c
        self._sauver()
        return nouvelles

    def modifier(self, id_: str, s: SaisieManuelle) -> CompetitionManuelle | None:
        ancienne = self.competitions.get(id_)
        if not ancienne:
            return None
        c = CompetitionManuelle(**s.model_dump(), id=id_, cree_le=ancienne.cree_le, modifie_le=datetime.now().astimezone())
        self.competitions[id_] = c
        self._sauver()
        return c

    def supprimer(self, id_: str) -> bool:
        if self.competitions.pop(id_, None) is None:
            return False
        self._sauver()
        return True


# --- Import CSV ---------------------------------------------------------------------------


def modele_csv() -> str:
    lignes = [
        COLONNES,
        ["Stage de Toussaint", "Savigny-sur-Orge", "26/10/2026", "28/10/2026", "Fleuret", "M11, M13, M15", "", "", "non", "", "non", "non", "Stage du club, gratuit"],
        ["Tournoi de Noël", "Massy", "12/12/2026", "", "Épée", "M13 à M17", "https://exemple.fr/invitation.pdf", "Invitation", "oui", "10", "oui", "oui", ""],
    ]
    sortie = io.StringIO()
    csv.writer(sortie, delimiter=";", lineterminator="\r\n").writerows(lignes)
    return "﻿" + sortie.getvalue()  # BOM : Excel ouvre le fichier en UTF-8


def _decoder(contenu: bytes) -> str:
    for encodage in ("utf-8-sig", "cp1252"):
        try:
            return contenu.decode(encodage)
        except UnicodeDecodeError:
            continue
    raise ValueError("Encodage du fichier non reconnu (UTF-8 ou Windows-1252 attendu)")


def _cle_colonne(entete: str) -> str:
    cle = re.sub(r"[^a-z0-9]+", "_", sans_accents(entete).lower()).strip("_")
    return SYNONYMES.get(cle, cle)


def _date(texte: str, champ: str) -> date | None:
    t = texte.strip()
    if not t:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{champ} : date illisible « {t} » (JJ/MM/AAAA attendu)")


def _booleen(texte: str, champ: str, defaut: bool) -> bool:
    t = sans_accents(texte).strip().upper()
    if not t:
        return defaut
    if t in VRAI:
        return True
    if t in FAUX:
        return False
    raise ValueError(f"{champ} : « {texte.strip()} » (oui ou non attendu)")


def _armes(texte: str) -> list[str]:
    armes = []
    for morceau in re.split(r"[,/;+]|\bET\b", sans_accents(texte).upper()):
        m = morceau.strip()
        if not m:
            continue
        code = m if m in ARMES else NOMS_ARMES.get(m) or next((k for n, k in NOMS_ARMES.items() if n.startswith(m)), None)
        if not code:
            raise ValueError(f"armes : « {morceau.strip()} » inconnue (Fleuret, Épée, Sabre…)")
        armes.append(code)
    return armes


def lire_csv(contenu: bytes) -> tuple[list[SaisieManuelle], list[str]]:
    """Retourne (compétitions valides, erreurs « ligne N : … »). Séparateur ; ou , détecté automatiquement."""
    if len(contenu) > TAILLE_MAX_CSV:
        raise ValueError("Fichier trop volumineux (1 Mo maximum)")
    texte = _decoder(contenu)
    premiere = texte.split("\n", 1)[0]
    separateur = ";" if premiere.count(";") >= premiere.count(",") else ","
    lecteur = csv.reader(io.StringIO(texte), delimiter=separateur)
    try:
        entetes = [_cle_colonne(e) for e in next(lecteur)]
    except StopIteration:
        raise ValueError("Fichier vide")
    manquantes = [c for c in ("nom", "lieu", "date_debut", "categories") if c not in entetes]
    if manquantes:
        raise ValueError(f"Colonne(s) obligatoire(s) absente(s) : {', '.join(manquantes)}. Colonnes attendues : {';'.join(COLONNES)}")

    saisies: list[SaisieManuelle] = []
    erreurs: list[str] = []
    for numero, ligne in enumerate(lecteur, start=2):
        if not any(cellule.strip() for cellule in ligne):
            continue
        if len(saisies) + len(erreurs) >= LIGNES_MAX_CSV:
            raise ValueError(f"Trop de lignes ({LIGNES_MAX_CSV} maximum)")
        v = {cle: (ligne[i] if i < len(ligne) else "") for i, cle in enumerate(entetes)}
        try:
            delai = v.get("delai_inscription_jours", "").strip()
            if delai and not delai.isdigit():
                raise ValueError(f"delai_inscription_jours : « {delai} » (nombre de jours attendu)")
            cats = categories_du_texte(v.get("categories", ""))
            if v.get("categories", "").strip() and not cats:
                raise ValueError(f"categories : « {v['categories'].strip()} » non reconnues (ex. M11, M13 ou M15 à Seniors)")
            saisies.append(SaisieManuelle(
                titre=v.get("nom", ""),
                lieu=v.get("lieu", ""),
                date_debut=_date(v.get("date_debut", ""), "date_debut"),
                date_fin=_date(v.get("date_fin", ""), "date_fin"),
                armes=_armes(v.get("armes", "")),
                categories=cats,
                document_url=v.get("document_url") or None,
                document_nom=v.get("document_nom") or None,
                preinscription=_booleen(v.get("preinscription", ""), "preinscription", True),
                delai_inscription_jours=int(delai) if delai else None,
                inscription_sur_place=_booleen(v.get("inscription_sur_place", ""), "inscription_sur_place", False),
                liee_ffe=_booleen(v.get("liee_ffe", ""), "liee_ffe", False),
                remarque=v.get("remarque") or None,
            ))
        except ValidationError as exc:
            details = "; ".join(f"{'.'.join(str(x) for x in e['loc']) or 'ligne'} : {e['msg']}" for e in exc.errors())
            erreurs.append(f"ligne {numero} : {details}")
        except ValueError as exc:
            erreurs.append(f"ligne {numero} : {exc}")
    return saisies, erreurs
