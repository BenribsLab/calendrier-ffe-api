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
| `POST /calendriers/{source}/cibles/{cible}` | Remplace un calendrier par un fichier déposé (`fichier`) ou un lien web (`lien`), formulaire multipart, jeton requis |
| `DELETE /calendriers/{source}/cibles/{cible}` | Revient au calendrier publié sur le site (jeton requis) |
| `GET /calendriers/{source}/fichiers/{fichier}` | Calendrier PDF archivé (dont les fichiers déposés) |

### Remplacer un calendrier à la main

Cibles : `cde91/cde91` (calendrier du CDE 91), `idf/fleuret`, `idf/epee`, `idf/sabre` (calendriers de la Ligue).

- Chaque cible est indépendante : remplacer le fleuret ne touche pas l'épée.
- Le nouveau calendrier est analysé **avant** d'écraser l'ancien : s'il ne contient aucune compétition pour la cible
  (ex. un calendrier épée déposé pour le fleuret), il est refusé et rien ne change.
- Le remplacement reste en place, même après l'analyse quotidienne, jusqu'au `DELETE` (retour au site).
  Il est enregistré dans `data/<source>/manuel.json` ; le fichier déposé est archivé dans `data/<source>/pdf/`.
- Chaque compétition donne dans `calendriers` le(s) calendrier(s) qui la mentionnent, avec leur lien :
  le fichier déposé (servi par l'API), le lien web saisi, ou le calendrier du site.

```bash
curl -X POST -H "Authorization: Bearer $JETON" -F "fichier=@Calendrier-Fleuret.pdf" https://calendrier-ffe.benribs.fr/calendriers/idf/cibles/fleuret
curl -X POST -H "Authorization: Bearer $JETON" -F "lien=https://…/Fleuret.pdf" https://calendrier-ffe.benribs.fr/calendriers/idf/cibles/fleuret
curl -X DELETE -H "Authorization: Bearer $JETON" https://calendrier-ffe.benribs.fr/calendriers/idf/cibles/fleuret
```

### Compétitions ajoutées à la main

Source `manuel` : compétitions saisies par le club (plugin WordPress, page « Compétitions ajoutées »), une par une ou
par import CSV. Enregistrées dans `data/manuelles.json`. Toujours incluses quand `source` n'est pas précisé.

| Route | Rôle |
|---|---|
| `GET /manuelles` | Liste (jeton requis) |
| `POST /manuelles` | Ajoute une compétition, JSON (jeton requis) |
| `PUT /manuelles/{id}` | Modifie (jeton requis) |
| `DELETE /manuelles/{id}` | Supprime (jeton requis) |
| `POST /manuelles/import` | Import CSV, champ `fichier` ; `essai=true` vérifie sans ajouter. Une ligne en erreur : rien n'est ajouté (jeton requis) |
| `GET /manuelles/modele.csv` | Modèle CSV |

Champs : `titre`, `lieu`, `date_debut` et `categories` obligatoires ; `date_fin` (défaut : le jour même), `armes`
(vide : toutes les armes), `document_url`, `document_nom`, `preinscription` (défaut vrai), `delai_inscription_jours`
(hors délai à partir de N jours avant le début, à midi), `inscription_sur_place`, `liee_ffe`, `remarque`.

- Compétitions officielles pour le filtre `officielle`.
- `liee_ffe` : quand la compétition apparaît sur le site fédéral (dates qui se chevauchent, même lieu, même arme, une
  catégorie en commun), la fiche FFE la remplace et reçoit les informations du club (remarque, document, préinscription).
- CSV : séparateur `;` ou `,`, UTF-8 ou Windows-1252, dates `JJ/MM/AAAA`, armes `Fleuret, Épée` ou `FLE,EPE`,
  catégories `M11, M13` ou `M13 à M17`, booléens `oui` / `non`.

Filtres (tous facultatifs, listes séparées par des virgules) :

| Paramètre | Exemple | Remarque |
|---|---|---|
| `source` | `ffe,cde91,idf,manuel` | par défaut `CAL_DEFAULT_SOURCES` (les compétitions ajoutées à la main sont toujours incluses) |
| `arme` | `FLE,EPE` | FLE, EPE, SAB, LAS, ART |
| `categorie` | `M13,M15` | M5 … M20, SENIOR, V1 … V4 |
| `departement` | `91,78` | codes INSEE |
| `region` | `11` | codes INSEE (11 = Île-de-France) |
| `niveau` | `11` | code FFE (voir `/referentiel`), FFE uniquement |
| `ville` | `savigny` | tout ou partie du nom |
| `date_debut`, `date_fin` | `2026-11-01` | garde les compétitions qui chevauchent la période |
| `equipe` | `individuel` / `equipe` | FFE uniquement |
| `officielle` | `true` | compétitions officielles uniquement (voir ci-dessous) |
| `pres_de` | `Savigny-sur-Orge` | point de départ du filtre de distance : une commune… |
| `lat`, `lon` | `48.685`, `2.349` | …ou une position (ex. celle du navigateur) |
| `rayon` | `30` | distance maximale en km, à vol d'oiseau (demande `pres_de` ou `lat`/`lon`) |

**Distance** : chaque compétition a les coordonnées du centre de la commune de son lieu (`latitude`, `longitude`,
via geo.api.gouv.fr, gratuit et sans clé). Avec un point de départ, chaque compétition reçoit `distance_km` ;
avec un `rayon`, seules celles à cette distance ou moins sont gardées. Celles dont le lieu est inconnu
(« Lieu indéterminé », villes étrangères) sont alors écartées et comptées dans `sans_position`.

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


## Sécurité

- **Administration** : jeton `CAL_ADMIN_TOKEN` d'au moins 24 caractères (sinon les routes d'administration sont
  désactivées), comparé en temps constant ; après 10 jetons refusés, l'adresse IP est bloquée 15 minutes.
  Le jeton ne doit circuler qu'en HTTPS.
- **Téléchargements (anti-SSRF)** : l'API n'accepte que des liens `http(s)` et refuse toute adresse non publique
  (127.0.0.1, réseau Docker ou local, 169.254.169.254…), redirections comprises. Pages bornées à 5 Mo, PDF à 20 Mo.
- **Données extérieures** : les liens venus des sites (CDE, Ligue, FFE) sont filtrés (`http(s)` uniquement) ;
  les identifiants FFE sont validés ; le flux `.ics` écarte les caractères de contrôle.
- **En-têtes** : `Content-Security-Policy`, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` ;
  pas d'en-tête `Server`. Documentation désactivable (`CAL_DOCS=false`, recommandé en production).
- **Conteneur** : utilisateur non privilégié, système de fichiers en lecture seule (sauf `/data` et `/tmp`),
  aucune capacité Linux, `no-new-privileges`, mémoire / CPU / processus limités, port publié sur `127.0.0.1` seulement.
- **Dépendances** : `pdfminer.six >= 20251107` (CVE-2025-64512). Reconstruire l'image régulièrement
  (`docker compose build --pull`) pour les correctifs du système et des bibliothèques.
- **Apache (reverse proxy)** : rediriger HTTP vers HTTPS et ajouter `Strict-Transport-Security`.

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
