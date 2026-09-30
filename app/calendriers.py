"""Calendriers publiés en PDF (CDE 91, Ligue d'Île-de-France).

Mécanisme commun : chaque jour, on relit la page qui liste les calendriers (le nom des fichiers change à chaque
version), on télécharge, on ne ré-analyse que si un fichier (empreinte SHA-256), un lien ou la version de l'analyse
a changé, et on archive chaque version téléchargée dans <data_dir>/<source>/pdf/.

Un « document » est un lien de la page : son libellé dit ce qu'il contient (« Calendrier IDF Fleuret 26-27 »).
Plusieurs liens peuvent pointer vers le même fichier : il n'est téléchargé qu'une fois, mais chaque lien est
analysé avec son propre libellé.
"""

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from .text import cle_ville, sans_accents

log = logging.getLogger(__name__)

LIEU_INDETERMINE = "Lieu indéterminé"


@dataclass(frozen=True)
class Document:
    url: str
    libelle: str = ""  # texte du lien, ex. « Calendrier IDF Fleuret 26-27 »


@dataclass
class Evenement:
    id: str
    titre: str
    lieu: str  # "" si le calendrier ne le donne pas
    date_debut: date
    date_fin: date
    armes: list[str]
    categories: list[str]
    categories_libelle: str
    horaire: str | None
    pdf_url: str


@dataclass
class Etat:
    pdfs: list[dict] = field(default_factory=list)  # url, libelle, sha256, fichier, analyse_le, nb_evenements
    evenements: list[Evenement] = field(default_factory=list)
    verifie_le: str | None = None
    erreur: str | None = None
    version: int = 0


def slug(texte: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", sans_accents(texte).lower()).strip("-")


def saison_en_cours(jour: date | None = None) -> tuple[int, int]:
    jour = jour or date.today()
    return (jour.year, jour.year + 1) if jour.month >= 8 else (jour.year - 1, jour.year)


def dedoublonner(evenements: list[Evenement]) -> list[Evenement]:
    uniques: dict[str, Evenement] = {}
    for e in evenements:
        uniques.setdefault(e.id, e)
    return list(uniques.values())


def lieu_connu(lieu: str) -> bool:
    return bool(lieu) and lieu != LIEU_INDETERMINE


def meme_lieu(a: str, b: str) -> bool:
    """'SAVIGNY S/ ORGE' = 'SAVIGNY-SUR-ORGE' ; 'St Gratien' = 'SAINT-GRATIEN' ; 'SAVIGNY' ~ 'SAVIGNY-SUR-ORGE'."""
    ka, kb = cle_ville(a), cle_ville(b)
    return bool(ka and kb) and (ka == kb or ka.startswith(kb) or kb.startswith(ka))


def meme_evenement(
    e: Evenement, date_debut: date, date_fin: date, lieu: str, armes: list[str], categories: list[str]
) -> bool:
    """Un événement d'un calendrier PDF correspond-il à une compétition déjà connue (FFE…) ?
    Dates qui se chevauchent (le calendrier de la Ligue ne donne que le week-end), même lieu, même arme et,
    si les deux les précisent, une catégorie en commun. Sans lieu, voir `Service._correspondances`."""
    return (
        e.date_debut <= date_fin
        and date_debut <= e.date_fin
        and lieu_connu(e.lieu)
        and meme_lieu(e.lieu, lieu)
        and bool(set(e.armes) & set(armes))
        and (not e.categories or not categories or bool(set(e.categories) & set(categories)))
    )


class CalendrierPDF:
    def __init__(
        self,
        nom: str,
        libelle: str,
        http: httpx.AsyncClient,
        page_url: str,
        dossier: Path,
        trouver_documents: Callable[[str, str], list[Document]],
        analyser: Callable[[bytes, Document], list[Evenement]],
        version: int,
    ):
        self.nom = nom
        self.libelle = libelle
        self.http = http
        self.page_url = page_url
        self.dossier = dossier / nom
        self.fichier_etat = self.dossier / "etat.json"
        self.trouver_documents = trouver_documents
        self.analyser = analyser
        self.version = version  # à incrémenter à chaque changement de l'analyse : les PDF connus sont ré-analysés
        self.etat = self._charger()

    def _charger(self) -> Etat:
        try:
            brut = json.loads(self.fichier_etat.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return Etat()
        except (OSError, ValueError):
            log.exception("%s : état illisible, on repart de zéro", self.libelle)
            return Etat()
        evenements = []
        for e in brut.get("evenements", []):
            e["date_debut"] = date.fromisoformat(e["date_debut"])
            e["date_fin"] = date.fromisoformat(e["date_fin"])
            evenements.append(Evenement(**e))
        etat = Etat(brut.get("pdfs", []), evenements, brut.get("verifie_le"), brut.get("erreur"), brut.get("version", 0))
        if etat.version != self.version:
            self._reanalyser_archives(etat)
        return etat

    def _reanalyser_archives(self, etat: Etat) -> None:
        """Nouvelle version de l'analyse : on relit tout de suite les PDF archivés, sans attendre le réseau."""
        try:
            evenements = []
            for info in etat.pdfs:
                contenu = (self.dossier / "pdf" / info["fichier"]).read_bytes()
                evenements.extend(self.analyser(contenu, Document(info["url"], info.get("libelle", ""))))
        except (KeyError, OSError, ValueError):
            log.warning("%s : PDF archivés indisponibles, ré-analyse au prochain rafraîchissement", self.libelle)
            return
        if evenements:
            etat.evenements, etat.version = dedoublonner(evenements), self.version
            log.info("%s : %d événements ré-analysés (version %d)", self.libelle, len(evenements), self.version)

    def _sauver(self) -> None:
        self.dossier.mkdir(parents=True, exist_ok=True)
        tmp = self.fichier_etat.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.etat), default=str, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.fichier_etat)

    async def rafraichir(self, forcer: bool = False) -> Etat:
        maintenant = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            r = await self.http.get(self.page_url)
            r.raise_for_status()
            documents = self.trouver_documents(r.text, str(r.url))
            if not documents:
                raise ValueError(f"Aucun calendrier trouvé sur {self.page_url}")

            anciens = {(p["url"], p.get("libelle", "")): p for p in self.etat.pdfs}
            change = forcer or {(d.url, d.libelle) for d in documents} != set(anciens) or self.etat.version != self.version
            contenus: dict[str, bytes] = {}
            pdfs = []
            for doc in documents:
                if doc.url not in contenus:  # un même fichier derrière plusieurs liens : un seul téléchargement
                    rp = await self.http.get(doc.url)
                    rp.raise_for_status()
                    contenus[doc.url] = rp.content
                empreinte = hashlib.sha256(contenus[doc.url]).hexdigest()
                if anciens.get((doc.url, doc.libelle), {}).get("sha256") != empreinte:
                    change = True
                pdfs.append({"url": doc.url, "libelle": doc.libelle, "sha256": empreinte})

            if not change:
                self.etat.verifie_le, self.etat.erreur = maintenant, None
                self._sauver()
                return self.etat

            evenements = []
            (self.dossier / "pdf").mkdir(parents=True, exist_ok=True)
            for info in pdfs:
                contenu = contenus[info["url"]]
                nom = f"{info['sha256'][:12]}_{Path(info['url']).name}"
                (self.dossier / "pdf" / nom).write_bytes(contenu)
                evs = self.analyser(contenu, Document(info["url"], info["libelle"]))
                info.update(fichier=nom, analyse_le=maintenant, nb_evenements=len(evs))
                evenements.extend(evs)

            if not evenements:
                raise ValueError("Calendrier analysé mais aucun événement reconnu (format changé ?)")
            self.etat = Etat(pdfs, dedoublonner(evenements), maintenant, None, self.version)
            log.info("%s : %d événements (%s)", self.libelle, len(self.etat.evenements),
                     ", ".join(f"{d.libelle or '?'} = {d.url}" for d in documents))
        except Exception as exc:
            # On garde les derniers événements connus, on signale l'erreur.
            log.exception("%s : rafraîchissement en échec", self.libelle)
            self.etat.verifie_le, self.etat.erreur = maintenant, str(exc) or type(exc).__name__
        self._sauver()
        return self.etat
