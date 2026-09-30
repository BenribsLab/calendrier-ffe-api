"""Calendriers de la Ligue d'Île-de-France (https://escrime-iledefrance.fr/liste-des-competitions/).

Un calendrier par arme (« Calendrier IDF Fleuret 26-27 », « Calendrier IDF Epée 26-27 », « Calendrier IDF Sabre 26-27 »)
et des calendriers M13 (« Calendrier IDF M13 Fleuret 26-27 », « … M13 Epée … »), chacun derrière un lien de la page.
L'arme (et M13) est lue dans le libellé du lien : c'est ce qui fait foi. Le fichier est un export PDF d'un classeur
Excel (« .xlsx.pdf ») dont le nom change à chaque version ; un même fichier peut contenir plusieurs pages (une par
arme) : pour un lien donné, on ne lit que la page de son arme, et pour un calendrier M13 que la colonne M13.
Chaque page est une grille : lignes = week-ends (« 5-6 », « 31-1 », « 6-7-8-9 »), colonnes = catégories
(Vétérans, Seniors Dames, Seniors Hommes, M20 … M13), mois écrits à la verticale (donc à l'envers
dans le texte extrait : « erbotcO »). Le mot « V A C A N C E S » est incrusté sur les week-ends de vacances.
Une case contient « Épreuve - Lieu » (« IDF 1 - St Gratien », « EN 1 - Rodez », « Chpt IDF Individuel - Melun »).
Le calendrier ne donne que le week-end : la date exacte vient de la FFE quand la compétition y est aussi.
"""

import io
import logging
import re
from datetime import date, timedelta
from urllib.parse import urljoin

import pdfplumber
from selectolax.parser import HTMLParser

from .calendriers import Document, Evenement, dedoublonner, saison_en_cours, slug
from .cde91 import armes as armes_du_texte
from .models import CATEGORIES
from .text import categories as extraire_categories
from .text import espaces, mois, sans_accents

log = logging.getLogger(__name__)

NOM = "idf"
LIBELLE = "Calendrier Ligue IDF"
VERSION_ANALYSE = 3  # à incrémenter à chaque changement de l'analyse


def liens_calendriers(html: str, page_url: str) -> list[Document]:
    """Un document par lien « Calendrier IDF … » de la page. Les titres sans lien (calendrier pas encore publié)
    sont ignorés : ils seront pris en compte dès que la Ligue y mettra un fichier."""
    arbre = HTMLParser(html)
    documents: list[Document] = []
    for a in arbre.css("a[href]"):
        libelle = espaces(a.text(strip=True))
        href = (a.attributes.get("href") or "").strip()
        chemin = sans_accents(href).lower().split("?")[0]
        if not sans_accents(libelle).lower().startswith("calendrier"):
            continue
        if not chemin.endswith((".pdf", ".xls", ".xlsx")):
            continue
        doc = Document(urljoin(page_url, href), libelle)
        if doc not in documents:
            documents.append(doc)
    return documents


# --- Nettoyage des cases ------------------------------------------------------------------

def _fragment_vacances(ligne: str) -> bool:
    """'V A', 'C A1', '/8 N C E', 'N C E S' : morceaux du mot VACANCES incrusté (avec d'éventuels débris)."""
    reste = re.sub(r"[\d/]", " ", ligne).split()
    return bool(reste) and all(len(j) == 1 and j in "VACNES" for j in reste)


def _joindre(lignes: list[str]) -> str:
    """Recolle les lignes d'une case : 'Maisons-' + 'Alfort' -> 'Maisons-Alfort', 'Melun -' + 'Bobigny' -> '… - Bobigny'."""
    texte = ""
    for ligne in lignes:
        ligne = ligne.strip()
        if not ligne:
            continue
        if texte.endswith("-") and not texte.endswith(" -"):
            texte += ligne
        else:
            texte = f"{texte} {ligne}" if texte else ligne
    return espaces(texte)


def nettoyer_case(texte: str | None) -> str:
    if not texte:
        return ""
    lignes = [l for l in texte.split("\n") if not _fragment_vacances(l)]
    t = _joindre(lignes)
    # Débris d'un texte voisin qui déborde : commence par une minuscule, ou lettre collée à un chiffre (« Cabries1/8 Fina »)
    if not t or t[0].islower() or re.search(r"[a-z]\d", t):
        return ""
    if not re.search(r"[A-Za-zÀ-ÿ]{2,}", t):
        return ""
    return t


# --- Libellés -----------------------------------------------------------------------------

_ABREVIATIONS = [
    (r"\bChpt\b\.?", "Championnat"),
    (r"\bind\.", "individuel"),
    (r"\bEQ\b|\bEq\b", "équipe"),
    (r"^EN\s*(\d)", r"Épreuve nationale \1"),
    (r"\bFDJ\b", "Fête des Jeunes"),
    (r"\bEDJ\b", "Entraînement des Jeunes"),
    (r"^EID\b", "Épreuve interdépartementale"),
    (r"^ED\b", "Épreuve départementale"),
    (r"(?i)^FRANCE\b(?!\s+ELITE)\s*-?\s*", "Championnat de France "),
]


def titre_complet(titre: str) -> str:
    for motif, remplacement in _ABREVIATIONS:
        titre = re.sub(motif, remplacement, titre)
    return espaces(titre)


def echelon(titre: str) -> str:
    t = sans_accents(titre).upper()
    if re.search(r"EPREUVE NATIONALE|CHAMPIONNAT DE FRANCE|^FRANCE|FRANCE ELITE|FETE DES JEUNES|FINALE", t):
        return "national"
    return "regional"


_DEPARTEMENTS = re.compile(r"[\s(-]*\(?\s*(\d{2}(?:\s*-\s*\d{2})+)\s*\)?\s*-?\s*$")
_DATE_PRECISE = re.compile(r"\s*-?\s*(\d{1,2})/(\d{1,2})\s*$")
# Cases sans « - Lieu » qui sont pourtant des épreuves (et non des villes)
_SANS_LIEU = re.compile(r"^(JNA|IDF\b|EN\s*\d|Chpt|FDJ|EDJ|1/[248]|Finale|FRANCE\b|France Elite|S[ée]lective|Challenge|Equipe)", re.IGNORECASE)


def decouper(texte: str) -> tuple[str, str, list[str], tuple[int, int] | None]:
    """'IDF 1 - St Gratien' -> ('IDF 1', 'St Gratien', [], None)
    '1/8 Finale FDJ - 78-92-95' -> ('1/8 Finale FDJ', '', ['78', '92', '95'], None)
    'Vincennes - 09/01' -> ('Vincennes', 'Vincennes', [], (9, 1))."""
    departements: list[str] = []
    m = _DEPARTEMENTS.search(texte)
    if m:
        departements = re.findall(r"\d{2}", m.group(1))
        texte = texte[: m.start()].strip()
    jour_mois = None
    m = _DATE_PRECISE.search(texte)
    if m:
        jour_mois = (int(m.group(1)), int(m.group(2)))
        texte = texte[: m.start()].strip()
    plusieurs_lieux = re.match(r"^(.*?)\s*-\s*\d+\s*lieux\s*-\s*(.+)$", texte, re.IGNORECASE)
    if plusieurs_lieux:  # « 1/8 Finale FDJ - 3 lieux - Bobigny - Vaureal »
        lieux = ", ".join(l.strip() for l in re.split(r"\s*-\s+|\s+-\s*", plusieurs_lieux.group(2)) if l.strip())
        return plusieurs_lieux.group(1).strip(" -"), lieux, departements, jour_mois
    morceaux = re.split(r"\s+-\s*|\s*-\s+", texte)
    if len(morceaux) == 1:
        # « … et équipe-Val d'Orge » : tiret sans espace devant un lieu en plusieurs mots
        # (mais pas « Maisons-Alfort », « Hénin-Beaumont » : pas d'espace après le tiret)
        colle = re.match(r"^(.*[a-zà-ÿ])-([A-ZÀ-Ý][^-]*[ '][^-]*)$", texte)
        if colle:
            morceaux = [colle.group(1), colle.group(2)]
    france = re.match(r"(?i)^FRANCE\s+(?!ELITE)([A-ZÀ-Ý].*)$", texte)
    if len(morceaux) > 1:
        titre, lieu = " - ".join(morceaux[:-1]).strip(), morceaux[-1].strip()
    elif france:  # « FRANCE Rennes »
        titre, lieu = "FRANCE", france.group(1)
    elif _SANS_LIEU.match(texte):
        titre, lieu = texte, ""
    else:
        # « Colmar », « Antony Open », « Hérouville + EQ » : la case ne contient que le lieu
        titre, lieu = texte, re.sub(r"\s*\+\s*(EQ|Eq|[ée]quipe)\s*$", "", texte)
    # « FRANCE - N2/N3 Castelnau le Lez » : la division fait partie du titre, pas du lieu
    division = re.match(r"^(N\d(?:\s*/\s*N\d)*)\s*-?\s*(.*)$", lieu)
    if division:
        titre, lieu = f"{titre} {division.group(1)}", division.group(2)
    return titre.strip(" -"), lieu.strip(" -"), departements, jour_mois


# --- Grille -------------------------------------------------------------------------------

def _mois_de(cellule: str) -> int | None:
    t = espaces(cellule or "").replace(" ", "")
    return mois(t[::-1]) or mois(t) if t else None


def _dates(cellule: str, mois_courant: int, saison: tuple[int, int]) -> tuple[date, date] | None:
    jours = [int(j) for j in re.findall(r"\d{1,2}", cellule or "")]
    if not jours:
        return None
    annee = saison[0] if mois_courant >= 8 else saison[1]
    try:
        debut = date(annee, mois_courant, jours[0])
    except ValueError:
        return None
    fin = debut
    for j in jours[1:]:  # jours consécutifs ; '31-1' passe au mois suivant
        suivant = fin + timedelta(days=1)
        while suivant.day != j and (suivant - fin).days < 7:
            suivant += timedelta(days=1)
        fin = suivant
    return debut, fin


def _saison(texte: str) -> tuple[int, int] | None:
    m = re.search(r"(20\d{2})\s*[/-]\s*(20\d{2})", texte)
    return (int(m.group(1)), int(m.group(2))) if m else None


def arme_de_la_page(page) -> tuple[list[str], str]:
    texte = page.extract_text() or ""
    titre_page = next((l for l in texte.splitlines() if "calendrier" in l.lower()), "")
    return armes_du_texte(titre_page), titre_page


TAILLE_MAX_TEXTE = 9  # le texte des cases fait ~6 pt ; l'incrustation « V A C A N C E S » 11,7 et 36 pt
ECART_MAX = 0.8  # entre deux caractères d'un même texte : -0,2 à 0,2 pt ; au-delà, ce sont deux textes


def _morceaux(chars: list[dict]) -> list[tuple[float, float, str]]:
    """Découpe les caractères d'une ligne en morceaux de texte continus : (top, centre x, texte).
    Deux textes qui se touchent sans espace (« Cabries » + « 1/8 Finale… ») sont séparés par leur écart."""
    morceaux = []
    courant: list[dict] = []
    for c in sorted(chars, key=lambda c: c["x0"]):
        if courant and c["x0"] - courant[-1]["x1"] > ECART_MAX:
            morceaux.append(courant)
            courant = []
        courant.append(c)
    if courant:
        morceaux.append(courant)
    resultat = []
    for m in morceaux:
        texte = "".join(c["text"] for c in m).strip()
        if texte:
            visibles = [c for c in m if c["text"].strip()]
            resultat.append((m[0]["top"], (visibles[0]["x0"] + visibles[-1]["x1"]) / 2, texte))
    return resultat


def textes_par_colonne(page, rangee, colonnes_x: dict[int, tuple[float, float]]) -> dict[int, str]:
    """Texte de chaque colonne d'une rangée. Un texte d'Excel centré qui déborde sur les cases voisines est rattaché
    à la colonne où il est centré (et non découpé entre plusieurs cases)."""
    # Hauteur de la rangée : celle de la case de date (« 5-6 »). La case du mois, fusionnée sur plusieurs
    # semaines, couvrirait tout le mois.
    case_date = rangee.cells[1] if len(rangee.cells) > 1 and rangee.cells[1] else None
    if case_date is None:
        return {}
    haut, bas = case_date[1], case_date[3]
    gauche = min(x0 for x0, _ in colonnes_x.values())
    chars = [
        c for c in page.chars
        if c["size"] < TAILLE_MAX_TEXTE and haut <= (c["top"] + c["bottom"]) / 2 <= bas and c["x1"] > gauche
    ]
    lignes: dict[int, list[dict]] = {}
    for c in chars:
        lignes.setdefault(round(c["top"]), []).append(c)
    par_colonne: dict[int, list[tuple[float, str]]] = {}
    for chars_ligne in lignes.values():
        for top, centre, texte in _morceaux(chars_ligne):
            for j, (x0, x1) in colonnes_x.items():
                if x0 <= centre < x1:
                    par_colonne.setdefault(j, []).append((top, texte))
                    break
    return {j: "\n".join(t for _, t in sorted(morceaux)) for j, morceaux in par_colonne.items()}


def analyser_page(page, pdf_url: str, saison_defaut: tuple[int, int]) -> list[Evenement]:
    armes, titre_page = arme_de_la_page(page)
    saison = _saison(titre_page) or saison_defaut
    tables = page.find_tables()
    if not tables or not armes:
        return []
    table = max(tables, key=lambda t: len(t.rows))
    grille = table.extract()

    # En-tête : la ligne qui contient les catégories
    colonnes: dict[int, list[str]] = {}
    debut_corps = 0
    for i, ligne in enumerate(grille):
        cats = {j: extraire_categories(c or "") for j, c in enumerate(ligne) if j >= 2}
        if sum(1 for v in cats.values() if v) >= 3:
            colonnes = {j: v for j, v in cats.items() if v}
            libelles = {j: espaces(ligne[j]) for j in colonnes}
            colonnes_x = {j: (table.rows[i].cells[j][0], table.rows[i].cells[j][2]) for j in colonnes}
            debut_corps = i + 1
            break
    if not colonnes:
        return []

    evenements: list[Evenement] = []
    mois_courant: int | None = None
    for i in range(debut_corps, len(grille)):
        ligne = grille[i]
        mois_courant = _mois_de(ligne[0]) or mois_courant
        if mois_courant is None or len(ligne) < 3:
            continue
        dates = _dates(ligne[1], mois_courant, saison)
        if not dates:
            continue
        # Une même épreuve sur plusieurs colonnes = une seule épreuve pour plusieurs catégories
        groupes: dict[str, list[int]] = {}
        for j, texte in textes_par_colonne(page, table.rows[i], colonnes_x).items():
            case = nettoyer_case(texte)
            for partie in re.split(r"\s+/\s+", case) if case else []:
                groupes.setdefault(partie, []).append(j)
        for texte_case, cols in groupes.items():
            titre, lieu, departements, jour_mois = decouper(texte_case)
            if not titre:
                continue
            debut, fin = dates
            if jour_mois:  # « Vincennes - 09/01 » : date précise
                for d in (debut + timedelta(days=k) for k in range((fin - debut).days + 1)):
                    if (d.day, d.month) == jour_mois:
                        debut = fin = d
            titre = titre_complet(titre)
            if departements:
                titre = f"{titre} ({'-'.join(departements)})"
            categories = sorted({c for j in cols for c in colonnes[j]}, key=CATEGORIES.index)
            evenements.append(
                Evenement(
                    id=f"{NOM}-{debut.isoformat()}-{slug(titre)}-{slug(lieu)}-{'-'.join(a.lower() for a in armes)}",
                    titre=titre,
                    lieu=lieu,
                    date_debut=debut,
                    date_fin=fin,
                    armes=armes,
                    categories=categories,
                    categories_libelle=", ".join(libelles[j] for j in cols),
                    horaire=None,
                    pdf_url=pdf_url,
                )
            )
    return evenements


def analyser_pdf(contenu: bytes, document: Document) -> list[Evenement]:
    """Analyse le calendrier d'un lien : seulement la page de l'arme du lien, et seulement M13 pour un calendrier M13."""
    if not contenu.startswith(b"%PDF"):
        raise ValueError(f"Format non pris en charge (PDF attendu) : {document.url}")
    arme_du_lien = armes_du_texte(document.libelle)
    m13 = bool(re.search(r"\bM\s*13\b", document.libelle, re.IGNORECASE))
    evenements: list[Evenement] = []
    with pdfplumber.open(io.BytesIO(contenu)) as pdf:
        for page in pdf.pages:
            armes_page, _ = arme_de_la_page(page)
            if arme_du_lien and armes_page != arme_du_lien:
                continue  # page d'une autre arme
            for e in analyser_page(page, document.url, saison_en_cours()):
                if m13:
                    if "M13" not in e.categories:
                        continue
                    e.categories, e.categories_libelle = ["M13"], "M13"
                evenements.append(e)
    if arme_du_lien and not evenements:
        log.warning("Aucune page %s trouvée dans %s (%s)", arme_du_lien, document.url, document.libelle)
    return dedoublonner(evenements)
