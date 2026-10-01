from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration, surchargeable par variables d'environnement préfixées CAL_ (ou fichier .env)."""

    model_config = SettingsConfigDict(env_prefix="CAL_", env_file=".env", extra="ignore")

    # URL publique de l'API (utilisée pour les liens dans les flux .ics)
    public_url: str = "https://calendrier-ffe.benribs.fr"
    user_agent: str = "calendrier-ffe.benribs.fr (+https://calendrier-ffe.benribs.fr)"
    http_timeout: float = 20.0

    # FFE
    ffe_base_url: str = "https://www.ffescrime.fr"
    ffe_list_ttl: int = 180  # secondes : cache court de la liste (temps réel)
    ffe_detail_ttl: int = 1800  # secondes : cache des fiches détail
    ffe_stale_max: int = 86400  # secondes : durée max de service d'une donnée périmée si la FFE est en panne

    # CDE 91
    cde91_page_url: str = "https://cde91.fr/index.php/2015-09-30-10-47-54/2015-09-30-10-48-13"
    cde91_departement: str = "91"
    cde91_region: str = "11"

    # Ligue d'Île-de-France
    idf_page_url: str = "https://escrime-iledefrance.fr/liste-des-competitions/"
    idf_region: str = "11"

    # Heure locale de l'analyse quotidienne des calendriers PDF (CDE 91, Ligue IDF)
    calendriers_refresh_hour: int = 6

    # Géolocalisation des villes (département / région)
    geo_api_url: str = "https://geo.api.gouv.fr"
    geo_concurrency: int = 8

    # Filtre "compétitions officielles" : tout le CDE 91 ; épreuves, championnats et H2036 FFE de cette région ;
    # circuits nationaux et championnats de France partout.
    officielle_region: str = "11"

    default_sources: str = "ffe,cde91,idf,manuel"
    data_dir: Path = Path("data")
    admin_token: str = ""  # requis pour les routes d'administration ; 24 caractères minimum, sinon elles sont désactivées
    docs: bool = True  # documentation interactive /docs, /redoc, /openapi.json (mettre false en production)
    cors_origins: str = "*"


@lru_cache
def get_settings() -> Settings:
    return Settings()
