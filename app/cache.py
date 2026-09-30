import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class Entree:
    valeur: Any
    stocke_a: float


@dataclass
class Resultat:
    valeur: Any
    stocke_a: float
    perime: bool  # True si la source a échoué et qu'on sert une ancienne valeur
    erreur: str | None = None


class CacheTTL:
    """Cache mémoire avec :
    - une durée de fraîcheur (ttl) ;
    - une seule requête en vol par clé (évite de marteler la source) ;
    - un repli sur la dernière valeur connue si la source échoue (jusqu'à stale_max).
    """

    def __init__(self, ttl: float, stale_max: float, max_entrees: int = 500):
        self.ttl = ttl
        self.stale_max = stale_max
        self.max_entrees = max_entrees
        self._donnees: dict[str, Entree] = {}
        self._verrous: dict[str, asyncio.Lock] = {}

    async def obtenir(self, cle: str, charger: Callable[[], Awaitable[Any]]) -> Resultat:
        entree = self._donnees.get(cle)
        if entree and time.time() - entree.stocke_a < self.ttl:
            return Resultat(entree.valeur, entree.stocke_a, False)

        verrou = self._verrous.setdefault(cle, asyncio.Lock())
        async with verrou:
            entree = self._donnees.get(cle)
            if entree and time.time() - entree.stocke_a < self.ttl:
                return Resultat(entree.valeur, entree.stocke_a, False)
            try:
                valeur = await charger()
            except Exception as exc:
                if entree and time.time() - entree.stocke_a < self.stale_max:
                    return Resultat(entree.valeur, entree.stocke_a, True, str(exc) or type(exc).__name__)
                raise
            self._ranger(cle, Entree(valeur, time.time()))
            return Resultat(valeur, self._donnees[cle].stocke_a, False)

    def _ranger(self, cle: str, entree: Entree) -> None:
        self._donnees[cle] = entree
        if len(self._donnees) > self.max_entrees:
            plus_ancienne = min(self._donnees, key=lambda k: self._donnees[k].stocke_a)
            self._donnees.pop(plus_ancienne, None)
            self._verrous.pop(plus_ancienne, None)
