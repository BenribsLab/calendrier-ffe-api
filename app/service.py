"""Assemble les sources (FFE en temps réel, calendriers PDF du CDE 91 et de la Ligue IDF, compétitions ajoutées à la
main par le club) et applique les filtres.

Une compétition = une arme : une fiche FFE (ou une ligne du CDE) qui regroupe plusieurs armes est éclatée en une
compétition par arme (identifiant suffixé « .EPE », « .FLE »…). Chaque arme est ainsi rapprochée, filtrée et jugée
officielle indépendamment (le calendrier épée de la Ligue ne rend pas officiel le fleuret d'une même fiche FFE).
"""

import logging
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from . import idf, manuelles
from .cache import CacheTTL
from .calendriers import LIEU_INDETERMINE, CalendrierPDF, Evenement, lieu_connu, meme_evenement, meme_lieu
from .config import Settings
from .ffe import ClientFFE, FicheFFE, Filtres, LigneFFE, jeton_depuis_id, type_et_echelon
from .geo import Geolocalisation, distance_km
from .manuelles import CompetitionManuelle, Manuelles
from .models import ARMES, Competition, CompetitionDetail, CompetitionList, LienCalendrier, PointDepart, SourceStatus
from .text import cle_ville, sans_accents

log = logging.getLogger(__name__)

SOURCES = ("ffe", "cde91", "idf", manuelles.NOM)
LIBELLES = {"ffe": "Calendrier FFE", "cde91": "Calendrier CDE 91", "idf": "Calendrier Ligue IDF", manuelles.NOM: manuelles.LIBELLE}

# "H2036 8eme de finale - 75-93", "1/8 finale Fête des Jeunes (77-91-94)" :
# compétition réservée aux départements listés en fin de titre.
_DEPARTEMENTS_TITRE = re.compile(r"(?:\s-\s*|\()\s*(\d{2,3}[AB]?(?:\s*-\s*\d{2,3}[AB]?)*)\s*\)?\s*$", re.IGNORECASE)


# Fiches que la fédération marque elle-même comme à ignorer (titre « NE PAS UTILISER »)
_A_IGNORER = re.compile(r"\bNE\s+PAS\s+UTILISER\b")


def a_ignorer(titre: str) -> bool:
    return bool(_A_IGNORER.search(sans_accents(titre or "").upper()))


def departements_du_titre(titre: str) -> list[str]:
    m = _DEPARTEMENTS_TITRE.search(titre)
    return re.findall(r"\d{2,3}[AB]?", m.group(1).upper()) if m else []


def lien_calendrier(source: str, e: Evenement) -> LienCalendrier:
    """Calendrier d'où vient l'événement : « Calendrier Ligue IDF – Fleuret », « Calendrier CDE 91 »."""
    libelle = LIBELLES[source]
    if source == "idf" and len(e.armes) == 1:
        libelle = f"{libelle} – {ARMES.get(e.armes[0], e.armes[0])}"
    return LienCalendrier(source=source, libelle=libelle, url=e.pdf_url)


def lieu_ffe(lieu: str) -> str:
    """La FFE note « Aucune localité » quand le lieu n'est pas encore fixé."""
    return LIEU_INDETERMINE if not lieu or sans_accents(lieu).lower() == "aucune localite" else lieu


def titres_compatibles(titre_ligue: str, titre_ffe: str) -> bool:
    """« IDF 2 » ~ « Epreuve Régional(e) 2 » ; « Championnat IDF » ~ « Championnat Régional(e) » ;
    « 1/8 Finale Fête des Jeunes » ~ « H2036 8eme de finale »."""
    t1, t2 = sans_accents(titre_ligue).upper(), sans_accents(titre_ffe).upper()
    if ("CHAMPIONNAT" in t1) != ("CHAMPIONNAT" in t2):
        return False
    if ("FETE DES JEUNES" in t1) != ("H2036" in t2):
        return False
    n1 = re.search(r"\b(?:IDF|NATIONALE)\s*(\d)\b", t1)
    n2 = re.search(r"\b(\d)\s*$", t2)
    return not (n1 and n2 and n1.group(1) != n2.group(1))


def id_arme(id_: str, arme: str) -> str:
    return f"{id_}.{arme}"


def separer_id(id_: str) -> tuple[str, str | None]:
    """'ffe-XXXX.EPE' -> ('ffe-XXXX', 'EPE') ; 'ffe-XXXX' -> ('ffe-XXXX', None)."""
    base, _, arme = id_.rpartition(".")
    return (base, arme) if base and arme in ARMES else (id_, None)


def eclater(c: Competition) -> list[Competition]:
    """Une compétition par arme."""
    if len(c.armes) <= 1:
        return [c]
    return [
        c.model_copy(update={"id": id_arme(c.id, a), "armes": [a], "sources": list(c.sources), "calendriers": list(c.calendriers)})
        for a in c.armes
    ]


def eclater_evenement(e: Evenement) -> list[Evenement]:
    if len(e.armes) <= 1:
        return [e]
    return [replace(e, id=id_arme(e.id, a), armes=[a]) for a in e.armes]


@dataclass
class Recherche(Filtres):
    sources: list[str] = field(default_factory=lambda: list(SOURCES))
    departements: list[str] = field(default_factory=list)  # codes INSEE
    regions: list[str] = field(default_factory=list)  # codes INSEE
    officielle: bool = False
    depart: PointDepart | None = None  # filtre de distance : point de départ et rayon (km)


class Service:
    def __init__(
        self, settings: Settings, ffe: ClientFFE, calendriers: dict[str, CalendrierPDF], geo: Geolocalisation,
        manuelles: Manuelles | None = None,
    ):
        self.settings = settings
        self.manuelles = manuelles or Manuelles(settings.data_dir)
        self.ffe = ffe
        self.calendriers = calendriers  # {"cde91": …, "idf": …}, dans l'ordre de fusion
        self.geo = geo
        self.cache_liste = CacheTTL(settings.ffe_list_ttl, settings.ffe_stale_max)
        self.cache_fiche = CacheTTL(settings.ffe_detail_ttl, settings.ffe_stale_max, max_entrees=2000)
        self.cache_referentiel = CacheTTL(86400, 7 * 86400)

    def url_note(self, id_: str) -> str:
        return f"{self.settings.public_url.rstrip('/')}/competitions/{id_}/note"

    # --- Conversion -------------------------------------------------------------------

    def _depuis_ffe(self, ligne: LigneFFE) -> Competition:
        return Competition(
            id=ligne.id,
            sources=["ffe"],
            titre=ligne.titre,
            lieu=lieu_ffe(ligne.lieu),
            date_debut=ligne.date_debut,
            date_fin=ligne.date_fin,
            armes=ligne.armes,
            categories=ligne.categories,
            categories_libelle=ligne.categories_libelle,
            type=ligne.type,
            echelon=ligne.echelon,
            url=self.ffe.url_fiche(ligne.id),
            note_url=self.url_note(ligne.id),
        )

    @staticmethod
    def _depuis_calendrier(source: str, e: Evenement) -> Competition:
        return Competition(
            id=e.id,
            sources=[source],
            titre=e.titre,
            lieu=e.lieu or LIEU_INDETERMINE,
            date_debut=e.date_debut,
            date_fin=e.date_fin,
            armes=e.armes,
            categories=e.categories,
            categories_libelle=e.categories_libelle,
            type="championnat" if "championnat" in e.titre.lower() else None,
            echelon="departemental" if source == "cde91" else idf.echelon(e.titre),
            horaire=e.horaire,
            url=e.pdf_url,
            calendriers=[lien_calendrier(source, e)],
        )

    @staticmethod
    def _depuis_manuelle(m: CompetitionManuelle) -> Competition:
        return Competition(
            id=m.id,
            sources=[manuelles.NOM],
            titre=m.titre,
            lieu=m.lieu,
            date_debut=m.date_debut,
            date_fin=m.date_fin,
            armes=m.armes,
            categories=m.categories,
            categories_libelle=manuelles.libelle_categories(m.categories),
            calendriers=Service._documents_manuelle(m),
            remarque=m.remarque,
            preinscription=m.preinscription,
            delai_inscription_jours=m.delai_inscription_jours,
            inscription_sur_place=m.inscription_sur_place,
            liee_ffe=m.liee_ffe,
            id_manuel=m.id,
        )

    @staticmethod
    def _documents_manuelle(m: CompetitionManuelle) -> list[LienCalendrier]:
        if not m.document_url:
            return []
        return [LienCalendrier(source=manuelles.NOM, libelle=m.document_nom or "Document", url=m.document_url)]

    @staticmethod
    def _meme_que_manuelle(m: Competition, c: Competition) -> bool:
        """Compétition FFE qui est la compétition « liée FFE » ajoutée à la main : dates qui se chevauchent, même lieu,
        même arme (toutes si la saisie n'en précise pas), une catégorie en commun."""
        return (
            "ffe" in c.sources
            and manuelles.NOM not in c.sources
            and m.date_debut <= c.date_fin and c.date_debut <= m.date_fin
            and lieu_connu(c.lieu) and meme_lieu(m.lieu, c.lieu)
            and (not m.armes or bool(set(m.armes) & set(c.armes)))
            and (not c.categories or bool(set(m.categories) & set(c.categories)))
        )

    @staticmethod
    def _fusionner_manuelle(c: Competition, m: Competition) -> None:
        """La fiche FFE fait référence (nom, dates, note) ; le club y ajoute ses informations."""
        c.sources.append(manuelles.NOM)
        for lien in m.calendriers:
            if lien not in c.calendriers:
                c.calendriers.append(lien)
        c.remarque = m.remarque
        c.preinscription = m.preinscription
        c.delai_inscription_jours = m.delai_inscription_jours
        c.inscription_sur_place = m.inscription_sur_place
        c.liee_ffe = True
        c.id_manuel = m.id_manuel

    def _competitions_manuelles(self) -> list[Competition]:
        return [x for m in self.manuelles.liste() for x in eclater(self._depuis_manuelle(m))]

    def _fusionner(self, c: Competition, source: str, e: Evenement) -> None:
        """Même compétition dans plusieurs sources : on garde la FFE (dates exactes, lieu, fiche, note).
        Le nom du CDE fait référence (un 'Tournoi' FFE qui est un 'Challenge de Référence' du CDE reste un CR)."""
        if source not in c.sources:
            c.sources.append(source)
        lien = lien_calendrier(source, e)
        if lien not in c.calendriers:
            c.calendriers.append(lien)
        if source == "cde91":
            c.titre = e.titre
        if source == "idf" and not c.region:
            c.region = self.settings.idf_region  # « Aucune localité » à la FFE, mais au calendrier de la Ligue
        c.horaire = c.horaire or e.horaire

    def _correspondances(self, source: str, e: Evenement, competitions: list[Competition]) -> list[Competition]:
        """Compétitions déjà connues qui sont le même événement (même arme, même catégorie, dates qui se chevauchent)."""
        if lieu_connu(e.lieu):
            return [
                c for c in competitions
                if meme_evenement(e, c.date_debut, c.date_fin, c.lieu, c.armes, c.categories)
                # La Ligue ne donne que le week-end : on ne rattache pas ses épreuves à une compétition
                # départementale FFE du même week-end (« IDF 1 - Clamart » ≠ « Épreuve Départementale 2 » à Clamart).
                and not (source == "idf" and "ffe" in c.sources and c.echelon == "departemental")
            ]
        # Lieu non précisé (« IDF 1 - ») : on cherche sur le site fédéral LA compétition du même week-end, même arme,
        # même catégorie, en Île-de-France ou elle aussi sans lieu (« Aucune localité » à la FFE).
        # Plusieurs candidates : on départage par le nom ; s'il en reste plusieurs, trop incertain, on ne rattache pas.
        candidates = [
            c for c in competitions
            if "ffe" in c.sources
            and (c.region == self.settings.idf_region or not lieu_connu(c.lieu))
            and c.echelon != "departemental"
            and e.date_debut <= c.date_fin and c.date_debut <= e.date_fin
            and set(e.armes) & set(c.armes)
            and e.categories and c.categories and set(e.categories) & set(c.categories)
        ]
        if len(candidates) > 1:
            candidates = [c for c in candidates if titres_compatibles(e.titre, c.titre)]
        return candidates if len(candidates) == 1 else []

    def _statut(self, cal: CalendrierPDF) -> SourceStatus:
        etat = cal.etat
        return SourceStatus(
            ok=etat.erreur is None and bool(etat.evenements),
            fetched_at=datetime.fromisoformat(etat.verifie_le) if etat.verifie_le else None,
            stale=etat.erreur is not None,
            message=etat.erreur,
        )

    # --- Recherche --------------------------------------------------------------------

    async def rechercher(self, r: Recherche) -> CompetitionList:
        statuts: dict[str, SourceStatus] = {}
        competitions: list[Competition] = []

        if "ffe" in r.sources:
            cle = "&".join(f"{k}={v}" for k, v in r.parametres_ffe())
            try:
                res = await self.cache_liste.obtenir(cle, lambda: self.ffe.liste(r))
                competitions += [c for l in res.valeur if not a_ignorer(l.titre) for c in eclater(self._depuis_ffe(l))]
                statuts["ffe"] = SourceStatus(
                    ok=not res.perime,
                    fetched_at=datetime.fromtimestamp(res.stocke_a, timezone.utc),
                    stale=res.perime,
                    message=res.erreur,
                )
            except Exception as exc:
                log.exception("FFE indisponible")
                statuts["ffe"] = SourceStatus(ok=False, message=f"FFE indisponible : {exc}")
        # La région des compétitions FFE sert au rapprochement des épreuves de la Ligue sans lieu.
        await self._geolocaliser(competitions)

        for source, cal in self.calendriers.items():
            if source not in r.sources:
                continue
            statuts[source] = self._statut(cal)
            nouvelles = []
            for e in (x for ev in cal.etat.evenements for x in eclater_evenement(ev)):
                doubles = self._correspondances(source, e, competitions)
                for c in doubles:
                    self._fusionner(c, source, e)
                if not doubles:
                    nouvelles.append(self._depuis_calendrier(source, e))
            await self._geolocaliser(nouvelles)
            competitions += nouvelles

        if manuelles.NOM in r.sources:
            statuts[manuelles.NOM] = SourceStatus(ok=True, fetched_at=datetime.now(timezone.utc))
            nouvelles = []
            for m in self._competitions_manuelles():
                fiches = [c for c in competitions if m.liee_ffe and self._meme_que_manuelle(m, c)]
                for c in fiches:
                    self._fusionner_manuelle(c, m)
                if not fiches:
                    nouvelles.append(m)
            await self._geolocaliser(nouvelles)
            competitions += nouvelles

        competitions = [c for c in competitions if not a_ignorer(c.titre) and self._garder(c, r)]
        for c in competitions:
            c.officielle = self.est_officielle(c)
        if r.officielle:
            competitions = [c for c in competitions if c.officielle]
        if r.departements or r.regions:
            competitions = [
                c for c in competitions
                if (r.departements and c.departement in r.departements) or (r.regions and c.region in r.regions)
            ]
        sans_position = 0
        if r.depart:
            for c in competitions:
                if c.latitude is not None and c.longitude is not None:
                    c.distance_km = round(distance_km(r.depart.latitude, r.depart.longitude, c.latitude, c.longitude), 1)
            if r.depart.rayon_km is not None:
                sans_position = sum(1 for c in competitions if c.distance_km is None)
                competitions = [c for c in competitions if c.distance_km is not None and c.distance_km <= r.depart.rayon_km]
        competitions.sort(key=lambda c: (c.date_debut, c.date_fin, c.lieu, c.titre, c.armes))
        return CompetitionList(
            count=len(competitions),
            generated_at=datetime.now(timezone.utc),
            sources=statuts,
            depart=r.depart,
            sans_position=sans_position,
            competitions=competitions,
        )

    @staticmethod
    def _garder(c: Competition, r: Recherche) -> bool:
        # Filtres locaux : indispensables pour les calendriers PDF, redondants (mais sans risque) pour la FFE.
        if r.armes and c.armes and not set(r.armes) & set(c.armes):  # sans arme (ajout manuel) : toutes
            return False
        if r.categories and c.categories and not set(r.categories) & set(c.categories):
            return False
        if r.date_debut and c.date_fin < r.date_debut:
            return False
        if r.date_fin and c.date_debut > r.date_fin:
            return False
        if r.ville and cle_ville(r.ville) not in cle_ville(c.lieu):
            return False
        if (r.equipe or r.niveau) and "ffe" not in c.sources:
            return False  # individuel / équipe et niveaux FFE : inconnus des calendriers PDF
        return True

    def est_officielle(self, c: Competition) -> bool:
        notre_departement = self.settings.cde91_departement
        reserve_a = departements_du_titre(c.titre)
        if reserve_a and notre_departement not in reserve_a:
            return False  # ex. "H2036 8eme de finale - 75-93", "1/8 finale Fête des Jeunes (78-92-95)"
        if "cde91" in c.sources or "idf" in c.sources or manuelles.NOM in c.sources:
            return True  # calendriers départemental et régional, compétitions ajoutées par le club
        if c.echelon == "national" and c.type in ("epreuve", "championnat"):
            return True  # circuits nationaux et championnats de France
        if c.region != self.settings.officielle_region:
            return False
        if c.echelon == "departemental":
            return False  # le départemental officiel, c'est le calendrier du CDE 91 (traité plus haut)
        return c.type in ("epreuve", "championnat") or c.titre.upper().startswith("H2036")

    async def _geolocaliser(self, competitions: list[Competition]) -> None:
        """Les calendriers PDF abrègent les villes ('SAVIGNY', 'St Maur') : on les cherche d'abord dans leur zone."""
        zones = {
            "cde91": {"departement": self.settings.cde91_departement},
            "idf": {"region": self.settings.idf_region},
        }
        defauts = {
            "cde91": (self.settings.cde91_departement, self.settings.cde91_region),
            "idf": (None, self.settings.idf_region),
        }
        par_zone: dict[str | None, list[Competition]] = {}
        for c in competitions:
            source = c.sources[0] if len(c.sources) == 1 and c.sources[0] in zones else None
            par_zone.setdefault(source, []).append(c)
        for source, liste in par_zone.items():
            geo = await self.geo.resoudre({c.lieu for c in liste if lieu_connu(c.lieu)}, **zones.get(source, {}))
            for c in liste:
                info = geo.get(c.lieu)
                if info:
                    c.departement, c.region = info.get("departement"), info.get("region")
                    c.latitude, c.longitude = info.get("lat"), info.get("lon")
                elif source in defauts:
                    c.departement, c.region = defauts[source]

    # --- Détail -----------------------------------------------------------------------

    async def fiche_ffe(self, id_: str) -> FicheFFE:
        res = await self.cache_fiche.obtenir(id_, lambda: self.ffe.fiche(id_))
        return res.valeur

    async def detail(self, id_: str) -> CompetitionDetail | None:
        base, arme = separer_id(id_)
        if base.startswith(f"{manuelles.NOM}-"):
            m = self.manuelles.get(base)
            if not m or (arme and arme not in m.armes):
                return None
            c = self._depuis_manuelle(m)
            if arme:
                c = c.model_copy(update={"id": id_, "armes": [arme]})
            await self._geolocaliser([c])
            c.officielle = self.est_officielle(c)
            return CompetitionDetail(**c.model_dump())
        for source, cal in self.calendriers.items():
            if base.startswith(f"{source}-"):
                e = next((e for e in cal.etat.evenements if e.id == base), None)
                if not e or (arme and arme not in e.armes):
                    return None
                if arme:
                    e = replace(e, id=id_, armes=[arme])
                c = self._depuis_calendrier(source, e)
                await self._geolocaliser([c])
                c.officielle = self.est_officielle(c)
                return CompetitionDetail(**c.model_dump())

        fiche = await self.fiche_ffe(base)
        if not fiche.date_debut or not (fiche.titre or fiche.lieu):
            return None  # la FFE renvoie une fiche vide (HTTP 200) pour un identifiant inconnu
        if a_ignorer(fiche.titre):
            return None
        if arme and arme not in fiche.armes:
            return None
        c = Competition(
            id=id_,
            sources=["ffe"],
            titre=fiche.titre,
            lieu=lieu_ffe(fiche.lieu),
            date_debut=fiche.date_debut,
            date_fin=fiche.date_fin or fiche.date_debut,
            armes=[arme] if arme else fiche.armes,
            categories=fiche.categories,
            categories_libelle=fiche.categories_libelle,
            url=self.ffe.url_fiche(base),
            note_url=self.url_note(base),
        )
        c.type, c.echelon = type_et_echelon(jeton_depuis_id(base))
        await self._geolocaliser([c])
        for source, cal in self.calendriers.items():
            for e in (x for ev in cal.etat.evenements for x in eclater_evenement(ev)):
                # Sans lieu, le rapprochement dépend des autres compétitions du week-end : réservé à la liste.
                if lieu_connu(e.lieu) and self._correspondances(source, e, [c]):
                    self._fusionner(c, source, e)
        m = next((m for m in self._competitions_manuelles() if m.liee_ffe and self._meme_que_manuelle(m, c)), None)
        if m:
            self._fusionner_manuelle(c, m)
        c.officielle = self.est_officielle(c)
        return CompetitionDetail(**c.model_dump(), note_organisation=fiche.note_organisation, site_web=fiche.site_web)

    async def referentiel(self) -> dict:
        async def charger():
            return {**await self.ffe.referentiel(), **await self.geo.decoupage()}

        res = await self.cache_referentiel.obtenir("ref", charger)
        ref = dict(res.valeur)
        ref["sources"] = [{"code": s, "libelle": LIBELLES[s]} for s in SOURCES]
        return ref


def liste_parametre(valeur: str | None, majuscules: bool = True) -> list[str]:
    if not valeur:
        return []
    items = [v.strip() for v in valeur.split(",") if v.strip()]
    return [sans_accents(v).upper() for v in items] if majuscules else items
