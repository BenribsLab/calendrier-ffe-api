"""Département / région d'une ville, via geo.api.gouv.fr.

La FFE ne donne que le nom de la ville et ses filtres région/département n'ont aucun effet :
on géolocalise nous-mêmes. Résultats mis en cache sur disque (une ville ne change pas de département).
Des corrections manuelles peuvent être ajoutées dans <data_dir>/geo_corrections.json :
    {"CHILLY MAZARIN": {"departement": "91", "region": "11"}}
"""

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

from .text import cle_ville, nom_developpe, sans_accents

log = logging.getLogger(__name__)

NON_TROUVE_RETENTE = 7 * 86400  # on retente une ville inconnue au bout d'une semaine


class Geolocalisation:
    def __init__(self, http: httpx.AsyncClient, api_url: str, dossier: Path, concurrence: int = 8):
        self.http = http
        self.api_url = api_url.rstrip("/")
        self.fichier = dossier / "geo_cache.json"
        self.fichier_corrections = dossier / "geo_corrections.json"
        self._semaphore = asyncio.Semaphore(concurrence)
        self._cache: dict[str, dict] = self._lire(self.fichier)
        self._corrections = {cle_ville(k): v for k, v in self._lire(self.fichier_corrections).items()}
        self._modifie = False

    @staticmethod
    def _lire(fichier: Path) -> dict:
        try:
            return json.loads(fichier.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.exception("Lecture impossible : %s", fichier)
            return {}

    def sauvegarder(self) -> None:
        if not self._modifie:
            return
        self.fichier.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.fichier.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._cache, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.fichier)
        self._modifie = False

    @staticmethod
    def _prefixe(departement: str | None, region: str | None) -> str:
        return f"{departement}:" if departement else f"r{region}:" if region else ""

    def _cle(self, ville: str, departement: str | None = None, region: str | None = None) -> str:
        cle = cle_ville(ville)
        return f"{self._prefixe(departement, region)}{cle}" if cle else cle

    def connu(self, ville: str, departement: str | None = None, region: str | None = None) -> dict | None:
        """Zone préférée : 'SAVIGNY' dans le PDF du CDE 91 est Savigny-sur-Orge, pas Savigny (69) ;
        'St Maur' dans le calendrier de la Ligue IDF est Saint-Maur-des-Fossés."""
        cle = cle_ville(ville)
        if cle in self._corrections:
            return self._corrections[cle]
        cles = [self._cle(ville, departement, region)] if (departement or region) else []
        for c in cles + [cle]:
            entree = self._cache.get(c)
            if entree and entree.get("departement"):
                return entree
        return None

    def _a_chercher(self, cle: str) -> bool:
        entree = self._cache.get(cle)
        return entree is None or (not entree.get("departement") and time.time() - entree.get("t", 0) > NON_TROUVE_RETENTE)

    async def resoudre(
        self, villes: set[str], departement: str | None = None, region: str | None = None
    ) -> dict[str, dict | None]:
        """Retourne {ville: {"departement", "region"} ou None}.
        Avec `departement` ou `region`, on cherche d'abord la commune dans cette zone, puis dans toute la France."""
        a_chercher: list[tuple[str, str | None, str | None]] = []
        for ville in villes:
            cle = cle_ville(ville)
            if not cle or cle in self._corrections:
                continue
            if (departement or region) and self._a_chercher(self._cle(ville, departement, region)):
                a_chercher.append((ville, departement, region))
            if self._a_chercher(cle):
                a_chercher.append((ville, None, None))
        if a_chercher:
            await asyncio.gather(*(self._chercher(v, d, r) for v, d, r in a_chercher))
            self.sauvegarder()
        return {v: self.connu(v, departement, region) for v in villes}

    async def _chercher(self, ville: str, departement: str | None = None, region: str | None = None) -> None:
        cle = cle_ville(ville)
        params = {"nom": nom_developpe(ville).title(), "fields": "nom,codeDepartement,codeRegion", "boost": "population", "limit": 5}
        if departement:
            params["codeDepartement"] = departement
        elif region:
            params["codeRegion"] = region
        async with self._semaphore:
            try:
                r = await self.http.get(f"{self.api_url}/communes", params=params)
                r.raise_for_status()
                communes = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("Géolocalisation impossible pour %r : %s", ville, exc)
                return  # erreur réseau : on ne mémorise rien, on retentera
        # Dans une zone donnée, on accepte aussi un nom partiel ('SAVIGNY' -> Savigny-sur-Orge).
        choix = self._choisir(cle, communes, partiel=bool(departement or region))
        self._cache[self._cle(ville, departement, region)] = {
            "nom": choix["nom"] if choix else None,
            "departement": choix.get("codeDepartement") if choix else None,
            "region": choix.get("codeRegion") if choix else None,
            "t": int(time.time()),
        }
        self._modifie = True

    async def decoupage(self) -> dict[str, list[dict[str, str]]]:
        """Régions et départements (codes INSEE), pour les listes de filtres."""
        regions = await self.http.get(f"{self.api_url}/regions", params={"fields": "nom,code"})
        departements = await self.http.get(f"{self.api_url}/departements", params={"fields": "nom,code,codeRegion"})
        regions.raise_for_status()
        departements.raise_for_status()
        return {
            "regions": sorted(
                ({"code": r["code"], "libelle": r["nom"]} for r in regions.json()), key=lambda r: sans_accents(r["libelle"])
            ),
            "departements": [
                {"code": d["code"], "libelle": f"{d['code']} – {d['nom']}", "region": d["codeRegion"]}
                for d in departements.json()
            ],
        }

    @staticmethod
    def _choisir(cle: str, communes: list[dict], partiel: bool = False) -> dict | None:
        # Nom identique d'abord (communes triées par population), sinon 'PARIS 13' -> 'Paris'.
        for c in communes:
            if cle_ville(c.get("nom", "")) == cle:
                return c
        for c in communes:
            nom = cle_ville(c.get("nom", ""))
            if nom and cle.startswith(nom) and cle[len(nom):].isdigit():
                return c
        if partiel:
            candidats = [c for c in communes if cle_ville(c.get("nom", "")).startswith(cle)]
            if candidats:
                return candidats[0]
        return None
