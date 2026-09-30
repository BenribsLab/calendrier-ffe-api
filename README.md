# calendrier-ffe-api

API des compétitions d'escrime, servie sur `https://calendrier-ffe.benribs.fr`.

| Source | Mode | Détail |
|---|---|---|
| Calendrier public FFE (ffescrime.fr/calendrier) | Temps réel, cache de 3 min | Lecture du HTML (il n'y a pas d'API officielle). Si la FFE ne répond pas, on sert la dernière version connue (max 24 h). |
| Calendrier départemental CDE 91 (PDF) | Tous les jours à 6 h | Le lien du PDF est lu sur la page du CDE (le nom du fichier change). Tableau : date, type, lieu, armes, catégories, horaire. |
| Calendriers de la Ligue d'Île-de-France (PDF) | Tous les jours à 6 h | Un calendrier par lien « Calendrier IDF … » de [la page des compétitions](https://escrime-iledefrance.fr/liste-des-competitions/) : Épée, Fleuret, Sabre, et M13 Épée / M13 Fleuret dès qu'un fichier y est lié. L'arme (et M13) est lue dans le libellé du lien ; dans le fichier, on ne lit que la page de cette arme (et que la colonne M13 pour un calendrier M13). Grille : week-ends en lignes, catégories en colonnes. |

Pour les calendriers PDF, on ne ré-analyse que si le contenu du fichier change ou si l'analyse elle-même a évolué
(constante `VERSION_ANALYSE` de chaque module, à incrémenter à chaque modification). Chaque version téléchargée est archivée
dans `data/<source>/pdf/`.

**Une compétition = une arme.** Une fiche FFE (ou une ligne du CDE) qui regroupe plusieurs armes est éclatée en une compétition
par arme (identifiant suffixé `.EPE`, `.FLE`…) : chaque arme est rapprochée, filtrée et jugée officielle séparément.

Quand une compétition apparaît dans plusieurs sources (même lieu, dates qui se chevauchent, même arme, une catégorie en commun),
on n'en garde qu'une : la version FFE (dates exactes, fiche, note d'organisation), complétée par l'horaire et, pour le CDE, par son nom.
Le calendrier de la Ligue ne donne que le week-end : une compétition qui n'y figure que là s'étend sur tout le week-end.

**Lieu indéterminé** : quand la Ligue ne donne pas le lieu (« IDF 1 - »), ou quand la FFE indique « Aucune localité ».
Pour une épreuve de la Ligue sans lieu, on cherche sur le site fédéral la compétition du même week-end, même arme, même catégorie,
en Île-de-France ou elle aussi sans lieu ; s'il y en a plusieurs, on départage par le nom (« IDF 2 » ~ « Épreuve Régionale 2 »,
championnat ~ championnat, Fête des Jeunes ~ H2036). Sinon, elle reste en « Lieu indéterminé ».

Champs gérés : **titre, lieu, dates, armes, catégories** (+ horaire quand le CDE le donne).
Le sexe n'est pas géré : on considère que chaque compétition est mixte.
Les notes d'organisation ne sont pas analysées, elles sont proposées au téléchargement.

## Ce qu'il faut savoir sur le site FFE

- Filtres qui marchent côté FFE : `arme[XXX]`, `categories` (une seule), `niveaux`, `date` / `final-date` (jj/mm/aaaa), `lieu_ville`, `individuelle_equipe`.
- **Sans effet côté FFE : `regions`, `departements`, `sexe`.** Le filtre département/région est donc fait par l'API : chaque ville est géolocalisée via [geo.api.gouv.fr](https://geo.api.gouv.fr), avec un cache définitif (`data/geo_cache.json`).
  Une ville mal reconnue peut être corrigée dans `data/geo_corrections.json` :
  `{"NOM DE LA VILLE": {"departement": "91", "region": "11"}}`
- La liste contient toutes les compétitions en une seule page, sans pagination.
- La fiche détail (`/competition/<base64>`) donne le lien de la note d'organisation (téléchargeable sans compte) et le site web de l'organisateur.

## Routes

Documentation interactive : `/docs`.

| Route | Rôle |
|---|---|
| `GET /competitions` | Liste filtrée (JSON) |
| `GET /competitions.ics` | Même liste en flux iCalendar, pour s'abonner (paramètre `nom` = nom de l'agenda) |
| `GET /competitions/{id}` | Détail, avec `note_organisation` et `site_web` |
| `GET /competitions/{id}/note` | Redirige vers la note d'organisation, ou vers la fiche FFE si elle n'est pas encore publiée |
| `GET /referentiel` | Valeurs possibles des filtres (armes, catégories, niveaux FFE) |
| `GET /calendriers` | Dernière analyse de chaque calendrier PDF (`cde91`, `idf`) |
| `GET /calendriers/{source}` | Dernière analyse d'un calendrier PDF |
| `POST /calendriers/{source}/refresh` | Relance l'analyse (en-tête `Authorization: Bearer <CAL_ADMIN_TOKEN>`) |

Filtres (tous facultatifs, listes séparées par des virgules) :

| Paramètre | Exemple | Remarque |
|---|---|---|
| `source` | `ffe,cde91,idf` | par défaut `CAL_DEFAULT_SOURCES` |
| `arme` | `FLE,EPE` | FLE, EPE, SAB, LAS, ART |
| `categorie` | `M13,M15` | M5 … M20, SENIOR, V1 … V4 |
| `departement` | `91,78` | codes INSEE |
| `region` | `11` | codes INSEE (11 = Île-de-France) |
| `niveau` | `11` | code FFE (voir `/referentiel`), FFE uniquement |
| `ville` | `savigny` | tout ou partie du nom |
| `date_debut`, `date_fin` | `2026-11-01` | garde les compétitions qui chevauchent la période |
| `equipe` | `individuel` / `equipe` | FFE uniquement |
| `officielle` | `true` | compétitions officielles uniquement (voir ci-dessous) |

**Compétitions officielles** (`officielle=true`, champ `officielle` dans chaque réponse) :
- tout le calendrier de la Ligue d'Île-de-France (sauf ce qui est réservé à d'autres départements, ex. « 1/8 finale Fête des Jeunes (78-92-95) ») ;
- tout le calendrier du CDE 91 : CR (Challenge de Référence), CF (Challenge de France, épreuve départementale), EDJ (Entraînement des Jeunes), challenges, stages… Ces abréviations sont affichées en toutes lettres ;
- en Île-de-France (`CAL_OFFICIELLE_REGION=11`) : les épreuves et championnats FFE, ainsi que le programme H2036, sauf :
  - toutes les compétitions **départementales** FFE (épreuves, championnats…) : le départemental officiel, c'est le calendrier du CDE 91 ;
  - les compétitions réservées à d'autres départements, repérées par la liste en fin de titre (« H2036 8eme de finale - 75-93 » est exclue, « … - 77-91-94 » est gardée) ;
  - les tournois et les événements (formations…) ;
- partout en France : les circuits nationaux (« Épreuve Nationale ») et les championnats de France (« Championnat National »).

Le type (`tournoi`, `epreuve`, `championnat`) et l'échelon (`departemental`, `regional`, `zone`, `national`, `international`) sont lus dans l'identifiant FFE.
Le filtre « niveau » de la FFE n'est pas fiable pour ça : la plupart des épreuves régionales y sont classées « Challenge / Open ».

Exemples :

```
/competitions?departement=91&arme=FLE,EPE
/competitions.ics?region=11&arme=FLE&categorie=M13,M15&nom=Fleuret%20M13-M15
```


## Lancer avec Docker

```bash
cp .env.example .env        # puis adapter (CAL_PUBLIC_URL, CAL_ADMIN_TOKEN…)
docker compose up -d --build
docker compose logs -f
```

L'API écoute sur `http://127.0.0.1:8765` (documentation : `/docs`).
Le port et l'adresse d'écoute sur la machine se règlent dans `.env` : `API_PORT` (8765 par défaut) et `API_BIND`
(`127.0.0.1` par défaut, `0.0.0.0` pour l'ouvrir au réseau). Pensez à garder `CAL_PUBLIC_URL` cohérent.
Les données (cache des villes, historique des PDF du CDE) sont dans le volume `calendrier-data`.
Le conteneur tourne avec **un seul worker** : les caches et la tâche quotidienne vivent dans le processus.

En production, Apache (ISPConfig) servira de reverse proxy vers `127.0.0.1:8765`. Cette configuration reste à écrire.

## Développement sans Docker

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"      # Windows : .venv\Scripts\pip
.venv/bin/pytest
.venv/bin/uvicorn app.main:app --reload --port 8765
```

Les tests tournent hors ligne, sur des pages et un PDF réels enregistrés dans `tests/fixtures/`.
Si la FFE ou le CDE change sa mise en page, il suffit d'enregistrer la nouvelle page à cet endroit et de relancer les tests.
