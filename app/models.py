from datetime import date, datetime

from pydantic import BaseModel, Field

ARMES = {
    "FLE": "Fleuret",
    "EPE": "Épée",
    "SAB": "Sabre",
    "LAS": "Sabre laser",
    "ART": "Artistique et spectacle",
}

# Ordre croissant, utilisé pour développer les plages ("M15 et plus", "M11 à Vétérans")
CATEGORIES = ["M5", "M7", "M9", "M11", "M13", "M15", "M17", "M20", "SENIOR", "V1", "V2", "V3", "V4"]


TYPES = {"tournoi": "Tournoi", "epreuve": "Épreuve", "championnat": "Championnat"}
ECHELONS = {
    "departemental": "Départemental",
    "regional": "Régional",
    "zone": "Zone",
    "national": "National",
    "international": "International",
}


class LienCalendrier(BaseModel):
    source: str
    libelle: str
    url: str = Field(description="Calendrier du site, fichier déposé (servi par l'API) ou lien web saisi")


class Competition(BaseModel):
    id: str
    sources: list[str]
    titre: str
    lieu: str
    departement: str | None = None  # code INSEE, ex. "91"
    region: str | None = None  # code INSEE, ex. "11" (Île-de-France)
    latitude: float | None = Field(None, description="Centre de la commune du lieu")
    longitude: float | None = None
    distance_km: float | None = Field(None, description="Distance à vol d'oiseau du point de départ (filtre de distance)")
    date_debut: date
    date_fin: date
    armes: list[str] = Field(description="Codes : " + ", ".join(ARMES))
    categories: list[str]
    categories_libelle: str | None = None
    type: str | None = Field(None, description="Codes : " + ", ".join(TYPES))
    echelon: str | None = Field(None, description="Codes : " + ", ".join(ECHELONS))
    officielle: bool = Field(False, description="Compétition officielle pour le club (voir le filtre officielle)")
    horaire: str | None = None
    url: str | None = Field(None, description="Fiche FFE ou PDF du CDE")
    note_url: str | None = Field(None, description="Lien vers la note d'organisation (redirige vers la fiche si non publiée)")
    calendriers: list[LienCalendrier] = Field(default_factory=list, description="Calendriers PDF (CDE 91, Ligue) qui la mentionnent")
    # Informations du club (compétitions ajoutées à la main, ou fiche FFE liée à l'une d'elles)
    remarque: str | None = None
    preinscription: bool | None = Field(None, description="false : pas de préinscription sur le site du club ; null : règle habituelle")
    delai_inscription_jours: int | None = Field(None, description="Hors délai à partir de N jours avant le début, à midi ; null : règle habituelle")
    inscription_sur_place: bool | None = None
    liee_ffe: bool | None = Field(None, description="Compétition ajoutée à la main qui sera publiée sur le site fédéral")
    id_manuel: str | None = Field(None, description="Compétition ajoutée à la main d'où viennent ces informations")


class CompetitionDetail(Competition):
    note_organisation: str | None = Field(None, description="Lien direct du PDF de la FFE, si publié")
    site_web: str | None = None


class SourceStatus(BaseModel):
    ok: bool
    fetched_at: datetime | None = None
    stale: bool = False
    message: str | None = None


class PointDepart(BaseModel):
    nom: str | None = Field(None, description="Commune saisie, ou vide pour une position GPS")
    latitude: float
    longitude: float
    rayon_km: float | None = None


class CompetitionList(BaseModel):
    count: int
    generated_at: datetime
    sources: dict[str, SourceStatus]
    depart: PointDepart | None = Field(None, description="Point de départ du filtre de distance")
    sans_position: int = Field(0, description="Compétitions écartées par le filtre de distance faute de lieu connu")
    competitions: list[Competition]
