"""Protections réseau et d'accès.

- Garde SSRF : l'API télécharge des adresses venues de l'extérieur (liens des pages du CDE et de la Ligue,
  liens saisis à la main, redirections). On refuse toute adresse qui n'est pas publique (127.0.0.1, réseau
  Docker, réseau local, métadonnées cloud 169.254.169.254…) pour qu'un site piraté ou un jeton volé ne
  puisse pas faire interroger le serveur lui-même ou le réseau interne.
- Téléchargements bornés en taille (un fichier géant ne doit pas saturer la mémoire).
- Liens : seuls http et https sont acceptés (pas de javascript:, file:, data:…).
- Limitation des tentatives de jeton d'administration par adresse IP.
"""

import asyncio
import ipaddress
import socket
import time
from urllib.parse import urlparse

import httpx

TAILLE_MAX_PAGE = 5 * 1024 * 1024
TAILLE_MAX_PDF = 20 * 1024 * 1024


class AdresseInterdite(httpx.RequestError):
    """Adresse non publique (réseau interne) : téléchargement refusé."""


def lien_web(url: str | None) -> str | None:
    """Garde un lien seulement s'il est en http(s) (écarte javascript:, file:, data:…)."""
    if not url:
        return None
    try:
        partie = urlparse(url.strip())
    except ValueError:
        return None
    return url.strip() if partie.scheme in ("http", "https") and partie.hostname else None


def adresse_publique(ip: str) -> bool:
    try:
        adresse = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(adresse, ipaddress.IPv6Address) and adresse.ipv4_mapped:
        adresse = adresse.ipv4_mapped
    return adresse.is_global and not adresse.is_multicast


class GardeReseau:
    """Hook httpx appelé avant CHAQUE requête (redirections comprises) : refuse les adresses non publiques."""

    def __init__(self, duree_cache: float = 300):
        self.duree_cache = duree_cache
        self._cache: dict[str, tuple[float, bool]] = {}

    async def _hote_public(self, hote: str) -> bool:
        maintenant = time.monotonic()
        connu = self._cache.get(hote)
        if connu and maintenant - connu[0] < self.duree_cache:
            return connu[1]
        try:
            ipaddress.ip_address(hote)
            adresses = [hote]
        except ValueError:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(hote, None, type=socket.SOCK_STREAM)
            except OSError:
                return True  # nom inconnu : la connexion échouera d'elle-même
            adresses = [info[4][0] for info in infos]
        public = bool(adresses) and all(adresse_publique(a) for a in adresses)
        self._cache[hote] = (maintenant, public)
        return public

    async def __call__(self, requete: httpx.Request) -> None:
        if requete.url.scheme not in ("http", "https"):
            raise AdresseInterdite(f"Protocole refusé : {requete.url.scheme}", request=requete)
        hote = requete.url.host
        if not hote or not await self._hote_public(hote):
            raise AdresseInterdite(f"Adresse non publique refusée : {hote}", request=requete)


async def telecharger(http: httpx.AsyncClient, url: str, limite: int) -> httpx.Response:
    """GET borné en taille : au-delà de `limite` octets, on abandonne (ValueError)."""
    async with http.stream("GET", url) as r:
        r.raise_for_status()
        annonce = r.headers.get("content-length")
        if annonce and annonce.isdigit() and int(annonce) > limite:
            raise ValueError(f"Fichier trop volumineux ({int(annonce) // 1024 // 1024} Mo, maximum {limite // 1024 // 1024} Mo)")
        morceaux, total = [], 0
        async for morceau in r.aiter_bytes():
            total += len(morceau)
            if total > limite:
                raise ValueError(f"Fichier trop volumineux (maximum {limite // 1024 // 1024} Mo)")
            morceaux.append(morceau)
    # Contenu déjà décompressé : on ne garde que le type (pour l'encodage du texte), pas Content-Encoding
    return httpx.Response(
        r.status_code,
        headers={"content-type": r.headers.get("content-type", "")},
        content=b"".join(morceaux),
        request=r.request,
    )


class LimiteTentatives:
    """Après `maximum` jetons refusés en `fenetre` secondes, l'adresse IP est bloquée jusqu'à la fin de la fenêtre."""

    def __init__(self, maximum: int = 10, fenetre: float = 900):
        self.maximum = maximum
        self.fenetre = fenetre
        self._echecs: dict[str, list[float]] = {}

    def _recents(self, ip: str) -> list[float]:
        limite = time.monotonic() - self.fenetre
        recents = [t for t in self._echecs.get(ip, []) if t > limite]
        if recents:
            self._echecs[ip] = recents
        else:
            self._echecs.pop(ip, None)
        return recents

    def bloquee(self, ip: str) -> bool:
        return len(self._recents(ip)) >= self.maximum

    def echec(self, ip: str) -> None:
        self._echecs.setdefault(ip, []).append(time.monotonic())
        if len(self._echecs) > 10000:  # borne mémoire
            self._echecs.pop(next(iter(self._echecs)))
