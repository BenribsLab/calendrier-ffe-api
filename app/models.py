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


class Competition(BaseModel):
    id: str
    sources: list[str]
    titre: str
    lieu: str
    departement: str | None = None  # code INSEE, ex. "91"
    region: str | None = None  # code INSEE, ex. "11" (Île-de-France)
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


class CompetitionDetail(Competition):
    note_organisation: str | None = Field(None, description="Lien direct du PDF de la FFE, si publié")
    site_web: str | None = None


class SourceStatus(BaseModel):
    ok: bool
    fetched_at: datetime | None = None
    stale: bool = False
    message: str | None = None


class CompetitionList(BaseModel):
    count: int
    generated_at: datetime
    sources: dict[str, SourceStatus]
    competitions: list[Competition]
