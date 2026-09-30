"""Calendriers publiés en PDF (CDE 91, Ligue d'Île-de-France).

Mécanisme commun : chaque jour, on relit la page qui liste les calendriers (le nom des fichiers change à chaque
version), on télécharge, on ne ré-analyse que si un fichier (empreinte SHA-256), un lien ou la version de l'analyse
a changé, et on archive chaque version téléchargée dans <data_dir>/<source>/pdf/.

Un « document » est un lien de la page : son libellé dit ce qu'il contient (« Calendrier IDF Fleuret 26-27 »).
Plusieurs liens peuvent pointer vers le même fichier : il n'est téléchargé qu'une fois, mais chaque lien est
analysé avec son propre libellé.

Remplacement manuel : chaque calendrier a des « cibles » (CDE 91 ; Ligue Fleuret, Épée, Sabre) qu'on peut remplacer
par un fichier déposé ou un lien web. Le remplacement ne touche que sa cible, reste en place jusqu'à ce qu'on le
retire (l'analyse quotidienne ne l'écrase pas) et est enregistré dans <data_dir>/<source>/manuel.json.
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
TAILLE_MAX_PDF = 20 * 1024 * 1024


@dataclass(frozen=True)
class Document:
    url: str  # adresse du calendrier (site, lien web, ou fichier déposé servi par l'API)
    libelle: str = ""  # texte du lien, ex. « Calendrier IDF Fleuret 26-27 »
    fichier: str | None = None  # fichier déposé : nom dans <source>/pdf/ (pas de téléchargement)


@dataclass(frozen=True)
class Cible:
    """Partie d'un calendrier remplaçable à la main (ex. « fleuret » de la Ligue)."""

    code: str
    libelle: str  # sert aussi de libellé de document pour l'analyse (« Calendrier IDF Fleuret »)
    remplace: Callable[[Document], bool]  # documents du site que ce remplacement écarte


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
    # url, libelle, sha256, fichier, analyse_le, nb_evenements, mode ("site" | "fichier" | "lien"), cible, nom
    pdfs: list[dict] = field(default_factory=list)
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


def _nom_fichier(nom: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(nom).name).strip("-.") or "calendrier.pdf"
    return base if base.lower().endswith(".pdf") else f"{base}.pdf"


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
        cibles: list[Cible] | None = None,
        url_publique: str = "",
    ):
        self.nom = nom
        self.libelle = libelle
        self.http = http
        self.page_url = page_url
        self.dossier = dossier / nom
        self.fichier_etat = self.dossier / "etat.json"
        self.fichier_manuel = self.dossier / "manuel.json"
        self.trouver_documents = trouver_documents
        self.analyser = analyser
        self.version = version  # à incrémenter à chaque changement de l'analyse : les PDF connus sont ré-analysés
        self.cibles = {c.code: c for c in cibles or []}
        self.url_publique = url_publique.rstrip("/")
        self.manuels: dict[str, dict] = self._lire_json(self.fichier_manuel)
        self.etat = self._charger()

    # --- Persistance ------------------------------------------------------------------

    @staticmethod
    def _lire_json(fichier: Path) -> dict:
        try:
            return json.loads(fichier.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.exception("Lecture impossible : %s", fichier)
            return {}

    def _ecrire_json(self, fichier: Path, donnees) -> None:
        self.dossier.mkdir(parents=True, exist_ok=True)
        tmp = fichier.with_suffix(".tmp")
        tmp.write_text(json.dumps(donnees, default=str, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(fichier)

    def _charger(self) -> Etat:
        brut = self._lire_json(self.fichier_etat)
        if not brut:
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

    def _sauver(self) -> None:
        self._ecrire_json(self.fichier_etat, asdict(self.etat))

    def _analyser_archives(self, pdfs: list[dict]) -> list[Evenement]:
        evenements = []
        for info in pdfs:
            contenu = (self.dossier / "pdf" / info["fichier"]).read_bytes()
            evenements.extend(self.analyser(contenu, Document(info["url"], info.get("libelle", ""))))
        return dedoublonner(evenements)

    def _reanalyser_archives(self, etat: Etat) -> None:
        """Nouvelle version de l'analyse : on relit tout de suite les PDF archivés, sans attendre le réseau."""
        try:
            evenements = self._analyser_archives(etat.pdfs)
        except (KeyError, OSError, ValueError):
            log.warning("%s : PDF archivés indisponibles, ré-analyse au prochain rafraîchissement", self.libelle)
            return
        if evenements:
            etat.evenements, etat.version = evenements, self.version
            log.info("%s : %d événements ré-analysés (version %d)", self.libelle, len(evenements), self.version)

    def _archiver(self, contenu: bytes, url_ou_nom: str) -> tuple[str, str]:
        empreinte = hashlib.sha256(contenu).hexdigest()
        nom = f"{empreinte[:12]}_{_nom_fichier(url_ou_nom.split('?')[0])}"
        (self.dossier / "pdf").mkdir(parents=True, exist_ok=True)
        (self.dossier / "pdf" / nom).write_bytes(contenu)
        return empreinte, nom

    def url_fichier(self, fichier: str) -> str:
        return f"{self.url_publique}/calendriers/{self.nom}/fichiers/{fichier}"

    def chemin_fichier(self, fichier: str) -> Path | None:
        """Fichier archivé servi par l'API (nom simple uniquement, pas de chemin)."""
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.pdf", fichier):
            return None
        chemin = self.dossier / "pdf" / fichier
        return chemin if chemin.is_file() else None

    # --- Remplacements manuels --------------------------------------------------------

    def _document_manuel(self, code: str) -> Document:
        m = self.manuels[code]
        cible = self.cibles[code]
        if m["mode"] == "lien":
            return Document(m["url"], cible.libelle)
        return Document(self.url_fichier(m["fichier"]), cible.libelle, m["fichier"])

    def _ecarte(self, doc: Document) -> bool:
        """Document du site remplacé par un calendrier manuel ?"""
        return any(self.cibles[code].remplace(doc) for code in self.manuels if code in self.cibles)

    def _documents(self, documents_site: list[Document]) -> list[tuple[Document, str | None]]:
        """Documents à analyser : ceux du site non remplacés, puis les calendriers manuels (avec leur cible)."""
        docs: list[tuple[Document, str | None]] = [(d, None) for d in documents_site if not self._ecarte(d)]
        docs += [(self._document_manuel(code), code) for code in self.manuels if code in self.cibles]
        return docs

    async def _contenu(self, doc: Document) -> bytes:
        if doc.fichier:
            return (self.dossier / "pdf" / doc.fichier).read_bytes()
        r = await self.http.get(doc.url)
        r.raise_for_status()
        return r.content

    async def remplacer(self, code: str, *, contenu: bytes | None = None, nom: str = "", lien: str | None = None) -> dict:
        """Remplace une cible par un fichier déposé (`contenu`, `nom`) ou un lien web (`lien`).
        Le calendrier est analysé AVANT d'écraser l'ancien : s'il ne contient aucune compétition pour la cible,
        il est refusé (ValueError) et rien ne change."""
        cible = self.cibles.get(code)
        if cible is None:
            raise KeyError(code)
        if lien:
            if not re.match(r"^https?://", lien):
                raise ValueError("Le lien doit commencer par http:// ou https://")
            r = await self.http.get(lien)
            r.raise_for_status()
            contenu, nom = r.content, lien
        if not contenu:
            raise ValueError("Fichier vide")
        if len(contenu) > TAILLE_MAX_PDF:
            raise ValueError("Fichier trop volumineux (20 Mo maximum)")
        if not contenu.startswith(b"%PDF"):
            raise ValueError("Ce n'est pas un fichier PDF")

        empreinte = hashlib.sha256(contenu).hexdigest()
        fichier = f"{empreinte[:12]}_{_nom_fichier((nom or 'calendrier.pdf').split('?')[0])}"
        doc = Document(lien, cible.libelle) if lien else Document(self.url_fichier(fichier), cible.libelle, fichier)
        evenements = self.analyser(contenu, doc)
        if not evenements:
            raise ValueError(f"Aucune compétition reconnue pour « {cible.libelle} » dans ce calendrier (mauvais fichier ?)")
        self._archiver(contenu, nom or "calendrier.pdf")  # seulement une fois validé

        maintenant = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.manuels[code] = {
            "mode": "lien" if lien else "fichier",
            "url": lien or self.url_fichier(fichier),
            "fichier": fichier,
            "nom": lien or Path(nom).name,
            "depose_le": maintenant,
        }
        self._ecrire_json(self.fichier_manuel, self.manuels)

        # Nouvel état, sans réseau : les autres cibles gardent leurs fichiers archivés.
        pdfs = [p for p in self.etat.pdfs if p.get("cible") != code and not (p.get("mode", "site") == "site" and cible.remplace(Document(p["url"], p.get("libelle", ""))))]
        pdfs.append({
            "url": doc.url, "libelle": doc.libelle, "sha256": empreinte, "fichier": fichier, "analyse_le": maintenant,
            "nb_evenements": len(evenements), "mode": self.manuels[code]["mode"], "cible": code, "nom": self.manuels[code]["nom"],
        })
        self.etat = Etat(pdfs, self._analyser_archives(pdfs), maintenant, None, self.version)
        self._sauver()
        log.info("%s : « %s » remplacé par %s (%d événements)", self.libelle, cible.libelle, self.manuels[code]["nom"], len(evenements))
        return {"cible": code, "nb_evenements": len(evenements)}

    async def retirer_remplacement(self, code: str) -> Etat:
        """Revient au calendrier publié sur le site pour cette cible."""
        if code not in self.cibles:
            raise KeyError(code)
        if self.manuels.pop(code, None) is not None:
            self._ecrire_json(self.fichier_manuel, self.manuels)
        return await self.rafraichir(forcer=True)

    def etat_cibles(self) -> list[dict]:
        """Pour chaque cible : calendrier actuellement utilisé (site, fichier déposé ou lien)."""
        resultat = []
        for code, cible in self.cibles.items():
            m = self.manuels.get(code)
            if m:
                utilise = [{"url": m["url"], "nom": m["nom"]}]
            else:
                utilise = [
                    {"url": p["url"], "nom": p.get("libelle") or Path(p["url"]).name}
                    for p in self.etat.pdfs
                    if p.get("mode", "site") == "site" and cible.remplace(Document(p["url"], p.get("libelle", "")))
                ]
            resultat.append({
                "cible": code,
                "libelle": cible.libelle,
                "mode": m["mode"] if m else "site",
                "depuis": m["depose_le"] if m else None,
                "calendriers": utilise,
            })
        return resultat

    # --- Rafraîchissement quotidien ---------------------------------------------------

    async def rafraichir(self, forcer: bool = False) -> Etat:
        maintenant = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            r = await self.http.get(self.page_url)
            r.raise_for_status()
            documents = self._documents(self.trouver_documents(r.text, str(r.url)))
            if not documents:
                raise ValueError(f"Aucun calendrier trouvé sur {self.page_url}")

            anciens = {(p["url"], p.get("libelle", "")): p for p in self.etat.pdfs}
            cles = {(d.url, d.libelle) for d, _ in documents}
            change = forcer or cles != set(anciens) or self.etat.version != self.version
            contenus: dict[str, bytes] = {}
            pdfs = []
            for doc, code in documents:
                if doc.url not in contenus:  # un même fichier derrière plusieurs liens : un seul téléchargement
                    contenus[doc.url] = await self._contenu(doc)
                empreinte = hashlib.sha256(contenus[doc.url]).hexdigest()
                if anciens.get((doc.url, doc.libelle), {}).get("sha256") != empreinte:
                    change = True
                info = {"url": doc.url, "libelle": doc.libelle, "sha256": empreinte, "mode": "site"}
                if code:
                    info.update(mode=self.manuels[code]["mode"], cible=code, nom=self.manuels[code]["nom"])
                pdfs.append(info)

            if not change:
                self.etat.verifie_le, self.etat.erreur = maintenant, None
                self._sauver()
                return self.etat

            evenements = []
            for (doc, _), info in zip(documents, pdfs):
                contenu = contenus[doc.url]
                fichier = doc.fichier or self._archiver(contenu, doc.url)[1]  # fichier déposé : déjà archivé
                evs = self.analyser(contenu, doc)
                info.update(fichier=fichier, analyse_le=maintenant, nb_evenements=len(evs))
                evenements.extend(evs)

            if not evenements:
                raise ValueError("Calendrier analysé mais aucun événement reconnu (format changé ?)")
            self.etat = Etat(pdfs, dedoublonner(evenements), maintenant, None, self.version)
            log.info("%s : %d événements (%s)", self.libelle, len(self.etat.evenements),
                     ", ".join(f"{d.libelle or '?'} = {d.url}" for d, _ in documents))
        except Exception as exc:
            # On garde les derniers événements connus, on signale l'erreur.
            log.exception("%s : rafraîchissement en échec", self.libelle)
            self.etat.verifie_le, self.etat.erreur = maintenant, str(exc) or type(exc).__name__
        self._sauver()
        return self.etat
