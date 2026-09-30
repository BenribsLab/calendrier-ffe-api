import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response

from . import ics
from . import cde91, idf
from .calendriers import CalendrierPDF
from .config import Settings, get_settings
from .ffe import ClientFFE
from .geo import Geolocalisation
from .models import ARMES, CATEGORIES, CompetitionDetail, CompetitionList
from .service import SOURCES, Recherche, Service, liste_parametre

log = logging.getLogger("calendrier_ffe")


async def _tache_quotidienne(service: Service, heure: int) -> None:
    """Analyse du PDF CDE 91 au démarrage puis chaque jour à `heure`, et préchauffage du cache des villes."""
    while True:
        for cal in service.calendriers.values():
            await cal.rafraichir()
        try:
            await service.rechercher(Recherche(sources=["ffe"]))  # géolocalise toutes les villes de la liste FFE
        except Exception:
            log.exception("Préchauffage FFE en échec")
        maintenant = datetime.now()
        prochain = maintenant.replace(hour=heure, minute=0, second=0, microsecond=0)
        if prochain <= maintenant:
            prochain += timedelta(days=1)
        await asyncio.sleep((prochain - maintenant).total_seconds())


def creer_app(
    settings: Settings | None = None,
    demarrer_taches: bool = True,
    transport: httpx.AsyncBaseTransport | None = None,  # pour les tests
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            timeout=settings.http_timeout,
            follow_redirects=True,
            transport=transport,
        ) as http:
            app.state.service = Service(
                settings,
                ClientFFE(http, settings.ffe_base_url),
                {
                    cde91.NOM: CalendrierPDF(
                        cde91.NOM, cde91.LIBELLE, http, settings.cde91_page_url, settings.data_dir,
                        cde91.liens_pdf, cde91.analyser_pdf, cde91.VERSION_ANALYSE,
                    ),
                    idf.NOM: CalendrierPDF(
                        idf.NOM, idf.LIBELLE, http, settings.idf_page_url, settings.data_dir,
                        idf.liens_calendriers, idf.analyser_pdf, idf.VERSION_ANALYSE,
                    ),
                },
                Geolocalisation(http, settings.geo_api_url, settings.data_dir, settings.geo_concurrency),
            )
            tache = asyncio.create_task(_tache_quotidienne(app.state.service, settings.calendriers_refresh_hour)) if demarrer_taches else None
            yield
            if tache:
                tache.cancel()

    app = FastAPI(
        title="Calendrier escrime – FFE & CDE 91",
        version="0.1.0",
        description=(
            "Compétitions du calendrier public de la FFE (lu en temps réel, cache court) "
            "et du calendrier départemental du CDE 91 (PDF analysé chaque jour)."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    def service(request: Request) -> Service:
        return request.app.state.service

    def recherche(
        source: str | None = Query(None, description="ffe,cde91,idf"),
        arme: str | None = Query(None, description="Codes séparés par des virgules : " + ",".join(ARMES)),
        categorie: str | None = Query(None, description=",".join(CATEGORIES)),
        departement: str | None = Query(None, description="Codes INSEE, ex. 91,78"),
        region: str | None = Query(None, description="Codes INSEE, ex. 11 (Île-de-France)"),
        niveau: str | None = Query(None, description="Code niveau FFE (voir /referentiel)"),
        ville: str | None = Query(None, description="Tout ou partie du nom de la ville"),
        date_debut: date | None = Query(None, description="AAAA-MM-JJ"),
        date_fin: date | None = Query(None, description="AAAA-MM-JJ"),
        equipe: str | None = Query(None, pattern="^(individuel|equipe)$"),
        officielle: bool = Query(
            False,
            description="Compétitions officielles uniquement : tout le CDE 91 ; épreuves, championnats et H2036 "
            "en Île-de-France ; circuits nationaux et championnats de France partout",
        ),
    ) -> Recherche:
        sources = liste_parametre(source or settings.default_sources, majuscules=False)
        inconnues = set(sources) - set(SOURCES)
        if inconnues:
            raise HTTPException(422, f"Source(s) inconnue(s) : {', '.join(sorted(inconnues))}")
        armes = liste_parametre(arme)
        if set(armes) - set(ARMES):
            raise HTTPException(422, f"Arme(s) inconnue(s). Codes valides : {', '.join(ARMES)}")
        categories = liste_parametre(categorie)
        if set(categories) - set(CATEGORIES):
            raise HTTPException(422, f"Catégorie(s) inconnue(s). Codes valides : {', '.join(CATEGORIES)}")
        if date_debut and date_fin and date_fin < date_debut:
            raise HTTPException(422, "date_fin est antérieure à date_debut")
        return Recherche(
            armes=armes,
            categories=categories,
            niveau=niveau,
            date_debut=date_debut,
            date_fin=date_fin,
            ville=ville,
            equipe=equipe,
            sources=sources,
            departements=liste_parametre(departement),
            regions=liste_parametre(region),
            officielle=officielle,
        )

    @app.get("/health", include_in_schema=False)
    async def health():
        return {"ok": True}

    @app.get("/competitions", response_model=CompetitionList, tags=["Compétitions"])
    async def competitions(r: Recherche = Depends(recherche), s: Service = Depends(service)):
        """Liste filtrée des compétitions (mêmes filtres que /competitions.ics)."""
        return await s.rechercher(r)

    @app.get("/competitions.ics", tags=["Compétitions"], response_class=Response)
    async def competitions_ics(
        nom: str = Query("Compétitions d'escrime", description="Nom du calendrier affiché dans l'agenda"),
        r: Recherche = Depends(recherche),
        s: Service = Depends(service),
    ):
        """Flux iCalendar à ajouter dans Google Agenda (« À partir de l'URL »), Apple ou Outlook."""
        resultat = await s.rechercher(r)
        corps = ics.generer(resultat.competitions, nom, urlparse(settings.public_url).hostname or "calendrier-ffe")
        return Response(
            corps,
            media_type="text/calendar; charset=utf-8",
            headers={"Content-Disposition": 'inline; filename="competitions.ics"', "Cache-Control": "public, max-age=300"},
        )

    @app.get("/competitions/{id_}", response_model=CompetitionDetail, tags=["Compétitions"])
    async def competition(id_: str, s: Service = Depends(service)):
        """Détail d'une compétition, avec le lien direct de la note d'organisation si elle est publiée."""
        try:
            detail = await s.detail(id_)
        except ValueError:
            raise HTTPException(404, "Identifiant inconnu")
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"FFE indisponible : {exc}")
        if not detail:
            raise HTTPException(404, "Compétition introuvable")
        return detail

    @app.get("/competitions/{id_}/note", tags=["Compétitions"], status_code=302, response_class=RedirectResponse)
    async def note(id_: str, s: Service = Depends(service)):
        """Redirige vers la note d'organisation (PDF FFE), ou vers la fiche FFE si elle n'est pas encore publiée."""
        try:
            detail = await s.detail(id_)
        except ValueError:
            raise HTTPException(404, "Identifiant inconnu")
        except httpx.HTTPError:
            if id_.startswith("ffe-"):
                return RedirectResponse(s.ffe.url_fiche(id_), status_code=302)
            raise
        if not detail:
            raise HTTPException(404, "Compétition introuvable")
        return RedirectResponse(detail.note_organisation or detail.url, status_code=302)

    @app.get("/referentiel", tags=["Référentiel"])
    async def referentiel(s: Service = Depends(service)):
        """Valeurs possibles des filtres (armes, catégories, niveaux FFE, régions, départements, sources)."""
        try:
            return await s.referentiel()
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"FFE indisponible : {exc}")

    def calendrier(source: str, s: Service = Depends(service)) -> CalendrierPDF:
        if source not in s.calendriers:
            raise HTTPException(404, f"Calendrier inconnu. Valeurs : {', '.join(s.calendriers)}")
        return s.calendriers[source]

    def etat_calendrier(cal: CalendrierPDF) -> dict:
        etat = cal.etat
        return {
            "source": cal.nom,
            "libelle": cal.libelle,
            "page": cal.page_url,
            "verifie_le": etat.verifie_le,
            "erreur": etat.erreur,
            "version_analyse": etat.version,
            "pdfs": etat.pdfs,
            "nb_evenements": len(etat.evenements),
        }

    @app.get("/calendriers", tags=["Calendriers PDF"])
    async def calendriers(s: Service = Depends(service)):
        """État de la dernière analyse de chaque calendrier PDF (CDE 91, Ligue IDF)."""
        return [etat_calendrier(cal) for cal in s.calendriers.values()]

    @app.get("/calendriers/{source}", tags=["Calendriers PDF"])
    async def calendrier_status(cal: CalendrierPDF = Depends(calendrier)):
        """État de la dernière analyse d'un calendrier PDF."""
        return etat_calendrier(cal)

    def admin(authorization: str = Header("")):
        attendu = f"Bearer {settings.admin_token}"
        if not settings.admin_token or not secrets.compare_digest(authorization, attendu):
            raise HTTPException(401, "Jeton d'administration requis")

    @app.post("/calendriers/{source}/refresh", tags=["Calendriers PDF"], dependencies=[Depends(admin)])
    async def calendrier_refresh(cal: CalendrierPDF = Depends(calendrier)):
        """Force une nouvelle analyse d'un calendrier PDF (en-tête Authorization: Bearer <CAL_ADMIN_TOKEN>)."""
        await cal.rafraichir(forcer=True)
        return etat_calendrier(cal)

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = creer_app()
