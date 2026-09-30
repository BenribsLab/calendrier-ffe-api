import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response

from . import ics
from . import cde91, idf
from .calendriers import CalendrierPDF
from .config import Settings, get_settings
from .ffe import ClientFFE
from .securite import TAILLE_MAX_PDF, GardeReseau, LimiteTentatives
from .geo import Geolocalisation
from .models import ARMES, CATEGORIES, CompetitionDetail, CompetitionList, PointDepart
from .service import SOURCES, Recherche, Service, liste_parametre

log = logging.getLogger("calendrier_ffe")

LONGUEUR_MIN_JETON = 24

# En-têtes de sécurité : l'API ne sert que du JSON, de l'iCalendar et des PDF (jamais de page à exécuter),
# sauf la documentation /docs qui a besoin de ses scripts.
ENTETES_SECURITE = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "cross-origin",
}
CSP_API = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


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
            max_redirects=5,
            transport=transport,
            # Garde SSRF sur chaque requête sortante, redirections comprises (sauf réseau simulé des tests)
            event_hooks={"request": [GardeReseau()]} if transport is None else None,
        ) as http:
            app.state.service = Service(
                settings,
                ClientFFE(http, settings.ffe_base_url),
                {
                    cde91.NOM: CalendrierPDF(
                        cde91.NOM, cde91.LIBELLE, http, settings.cde91_page_url, settings.data_dir,
                        cde91.liens_pdf, cde91.analyser_pdf, cde91.VERSION_ANALYSE,
                        cibles=cde91.CIBLES, url_publique=settings.public_url,
                    ),
                    idf.NOM: CalendrierPDF(
                        idf.NOM, idf.LIBELLE, http, settings.idf_page_url, settings.data_dir,
                        idf.liens_calendriers, idf.analyser_pdf, idf.VERSION_ANALYSE,
                        cibles=idf.CIBLES, url_publique=settings.public_url,
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
        docs_url="/docs" if settings.docs else None,
        redoc_url="/redoc" if settings.docs else None,
        openapi_url="/openapi.json" if settings.docs else None,
    )

    @app.middleware("http")
    async def entetes_securite(request: Request, call_next):
        reponse = await call_next(request)
        for nom, valeur in ENTETES_SECURITE.items():
            reponse.headers.setdefault(nom, valeur)
        if not request.url.path.startswith(("/docs", "/redoc")):
            reponse.headers.setdefault("Content-Security-Policy", CSP_API)
        return reponse

    if len(settings.admin_token) < LONGUEUR_MIN_JETON:
        log.warning(
            "CAL_ADMIN_TOKEN absent ou trop court (%d caractères minimum) : routes d'administration désactivées",
            LONGUEUR_MIN_JETON,
        )
    tentatives = LimiteTentatives()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
        allow_methods=["GET", "POST", "DELETE"],  # POST / DELETE : administration des calendriers (jeton)
        allow_headers=["*"],
    )

    def service(request: Request) -> Service:
        return request.app.state.service

    async def recherche(
        source: str | None = Query(None, max_length=40, description="ffe,cde91,idf"),
        arme: str | None = Query(None, max_length=40, description="Codes séparés par des virgules : " + ",".join(ARMES)),
        categorie: str | None = Query(None, max_length=80, description=",".join(CATEGORIES)),
        departement: str | None = Query(None, max_length=200, description="Codes INSEE, ex. 91,78"),
        region: str | None = Query(None, max_length=100, description="Codes INSEE, ex. 11 (Île-de-France)"),
        niveau: str | None = Query(None, max_length=10, pattern=r"^\d*$", description="Code niveau FFE (voir /referentiel)"),
        ville: str | None = Query(None, max_length=80, description="Tout ou partie du nom de la ville"),
        date_debut: date | None = Query(None, description="AAAA-MM-JJ"),
        date_fin: date | None = Query(None, description="AAAA-MM-JJ"),
        equipe: str | None = Query(None, pattern="^(individuel|equipe)$"),
        officielle: bool = Query(
            False,
            description="Compétitions officielles uniquement : tout le CDE 91 ; épreuves, championnats et H2036 "
            "en Île-de-France ; circuits nationaux et championnats de France partout",
        ),
        pres_de: str | None = Query(None, max_length=80, description="Filtre de distance : commune de départ (ex. Savigny-sur-Orge)"),
        lat: float | None = Query(None, ge=-90, le=90, description="…ou position de départ (latitude)"),
        lon: float | None = Query(None, ge=-180, le=180, description="…et longitude"),
        rayon: float | None = Query(None, gt=0, le=2000, description="Distance maximale en km, à vol d'oiseau"),
        s: Service = Depends(service),
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
        depart = None
        if (lat is None) != (lon is None):
            raise HTTPException(422, "Donner lat ET lon")
        if lat is not None:
            depart = PointDepart(latitude=lat, longitude=lon, rayon_km=rayon)
        elif pres_de and pres_de.strip():
            try:
                commune = await s.geo.commune(pres_de.strip())
            except httpx.HTTPError as exc:
                raise HTTPException(502, f"Recherche de la commune impossible : {exc}")
            if not commune:
                raise HTTPException(422, f"Commune inconnue : {pres_de}")
            depart = PointDepart(nom=commune["nom"], latitude=commune["lat"], longitude=commune["lon"], rayon_km=rayon)
        elif rayon is not None:
            raise HTTPException(422, "Le rayon demande un point de départ (pres_de, ou lat et lon)")
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
            depart=depart,
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
        nom: str = Query("Compétitions d'escrime", max_length=120, description="Nom du calendrier affiché dans l'agenda"),
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
            "cibles": cal.etat_cibles(),
        }

    @app.get("/calendriers", tags=["Calendriers PDF"])
    async def calendriers(s: Service = Depends(service)):
        """État de la dernière analyse de chaque calendrier PDF (CDE 91, Ligue IDF)."""
        return [etat_calendrier(cal) for cal in s.calendriers.values()]

    @app.get("/calendriers/{source}", tags=["Calendriers PDF"])
    async def calendrier_status(cal: CalendrierPDF = Depends(calendrier)):
        """État de la dernière analyse d'un calendrier PDF."""
        return etat_calendrier(cal)

    def admin(request: Request, authorization: str = Header("")):
        if len(settings.admin_token) < LONGUEUR_MIN_JETON:
            raise HTTPException(503, f"Administration désactivée : CAL_ADMIN_TOKEN absent ou trop court ({LONGUEUR_MIN_JETON} caractères minimum)")
        ip = request.client.host if request.client else "?"
        if tentatives.bloquee(ip):
            raise HTTPException(429, "Trop de jetons refusés depuis cette adresse : réessayez dans 15 minutes")
        if not secrets.compare_digest(authorization.encode(), f"Bearer {settings.admin_token}".encode()):
            tentatives.echec(ip)
            log.warning("Jeton d'administration refusé (IP %s, %s %s)", ip, request.method, request.url.path)
            raise HTTPException(401, "Jeton d'administration requis")

    @app.post("/calendriers/{source}/refresh", tags=["Calendriers PDF"], dependencies=[Depends(admin)])
    async def calendrier_refresh(cal: CalendrierPDF = Depends(calendrier)):
        """Force une nouvelle analyse d'un calendrier PDF (en-tête Authorization: Bearer <CAL_ADMIN_TOKEN>)."""
        await cal.rafraichir(forcer=True)
        return etat_calendrier(cal)

    @app.post("/calendriers/{source}/cibles/{cible}", tags=["Calendriers PDF"], dependencies=[Depends(admin)])
    async def calendrier_remplacer(
        cible: str,
        fichier: UploadFile | None = File(None, description="Calendrier PDF à déposer"),
        lien: str | None = Form(None, description="…ou lien web vers le calendrier PDF"),
        cal: CalendrierPDF = Depends(calendrier),
    ):
        """Remplace un calendrier (CDE 91 : `cde91` ; Ligue : `fleuret`, `epee`, `sabre`) par un fichier déposé ou un
        lien web. N'écrase que cette cible. Le calendrier est analysé avant : s'il ne contient aucune compétition
        reconnue pour la cible, il est refusé et l'ancien est conservé. Reste en place jusqu'au DELETE."""
        if cible not in cal.cibles:
            raise HTTPException(404, f"Cible inconnue. Valeurs : {', '.join(cal.cibles)}")
        if (fichier is None) == (not lien):
            raise HTTPException(422, "Donner soit un fichier, soit un lien (et pas les deux)")
        try:
            if fichier is not None:
                contenu = bytearray()
                while morceau := await fichier.read(1024 * 1024):
                    contenu += morceau
                    if len(contenu) > TAILLE_MAX_PDF:
                        raise HTTPException(413, "Fichier trop volumineux (20 Mo maximum)")
                bilan = await cal.remplacer(cible, contenu=bytes(contenu), nom=fichier.filename or "calendrier.pdf")
            else:
                bilan = await cal.remplacer(cible, lien=lien.strip())
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Lien injoignable : {exc}")
        return {**bilan, **etat_calendrier(cal)}

    @app.delete("/calendriers/{source}/cibles/{cible}", tags=["Calendriers PDF"], dependencies=[Depends(admin)])
    async def calendrier_retablir(cible: str, cal: CalendrierPDF = Depends(calendrier)):
        """Annule le remplacement manuel d'une cible : on revient au calendrier publié sur le site."""
        if cible not in cal.cibles:
            raise HTTPException(404, f"Cible inconnue. Valeurs : {', '.join(cal.cibles)}")
        await cal.retirer_remplacement(cible)
        return etat_calendrier(cal)

    @app.get("/calendriers/{source}/fichiers/{fichier}", tags=["Calendriers PDF"], response_class=FileResponse)
    async def calendrier_fichier(fichier: str, cal: CalendrierPDF = Depends(calendrier)):
        """Calendrier PDF archivé (dont les fichiers déposés à la main)."""
        chemin = cal.chemin_fichier(fichier)
        if chemin is None:
            raise HTTPException(404, "Fichier introuvable")
        return FileResponse(chemin, media_type="application/pdf", filename=fichier.split("_", 1)[-1],
                            content_disposition_type="inline")

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = creer_app()
