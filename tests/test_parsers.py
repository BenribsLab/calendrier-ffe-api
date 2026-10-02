from datetime import date
from pathlib import Path

import pytest

from app import calendriers, cde91, ffe, idf
from app.models import Competition
from app import ics
from app.text import categories, cle_ville, plage_dates_ffe

FIXTURES = Path(__file__).parent / "fixtures"


def lire(nom: str) -> str:
    return (FIXTURES / nom).read_text(encoding="utf-8")


# --- Textes -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "texte, attendu",
    [
        ("Du 04 au 04 octobre 2026", (date(2026, 10, 4), date(2026, 10, 4))),
        ("Du 22 septembre au 03 octobre 2026", (date(2026, 9, 22), date(2026, 10, 3))),
        ("Du 28 décembre au 02 janvier 2027", (date(2026, 12, 28), date(2027, 1, 2))),
        ("Du 28 décembre 2026 au 02 janvier 2027", (date(2026, 12, 28), date(2027, 1, 2))),
        ("  Du 29 au 29   novembre 2026 ", (date(2026, 11, 29), date(2026, 11, 29))),
    ],
)
def test_plage_dates(texte, attendu):
    assert plage_dates_ffe(texte) == attendu


@pytest.mark.parametrize(
    "texte, attendu",
    [
        ("M9, M11, M13", ["M9", "M11", "M13"]),
        ("SENIOR, M17, M13", ["M13", "M17", "SENIOR"]),
        ("M7 M9/M11 / M13", ["M7", "M9", "M11", "M13"]),
        ("M15 et plus", ["M15", "M17", "M20", "SENIOR", "V1", "V2", "V3", "V4"]),
        ("M11 à Vétérans", ["M11", "M13", "M15", "M17", "M20", "SENIOR", "V1", "V2", "V3", "V4"]),
        ("V1, V2, V3, V4", ["V1", "V2", "V3", "V4"]),
        ("A définir", []),
    ],
)
def test_categories(texte, attendu):
    assert categories(texte) == attendu


def test_cle_ville():
    assert cle_ville("SAVIGNY S/ ORGE") == cle_ville("Savigny-sur-Orge")
    assert cle_ville("CHILLY MAZARIN") == cle_ville("CHILLY-MAZARIN")
    assert cle_ville("ETAMPES") == cle_ville("Étampes")


# --- FFE --------------------------------------------------------------------------------


def test_id_reversible():
    jeton = "MS1zZX/BhcmF0b3+Itc2VwYXJh=="
    id_ = ffe.id_depuis_jeton(jeton)
    assert "/" not in id_ and "=" not in id_
    assert ffe.jeton_depuis_id(id_) == jeton


def test_liste_savigny():
    lignes = ffe.analyser_liste(lire("ffe_liste_savigny.html"))
    assert len(lignes) == 2
    a, b = lignes
    assert a.titre == "H2036 8eme de finale - 77-91-94"
    assert a.lieu == "SAVIGNY-SUR-ORGE"
    assert (a.date_debut, a.date_fin) == (date(2026, 10, 3), date(2026, 10, 3))
    assert a.armes == ["FLE"]
    assert a.categories == ["M15"]
    assert b.titre == "Tournoi"
    assert b.categories == ["M9", "M11", "M13"]


def test_liste_complete():
    lignes = ffe.analyser_liste(lire("ffe_liste_complete.html"))
    assert len(lignes) == 677
    assert len({l.id for l in lignes}) == 677
    assert all(l.armes for l in lignes)
    assert all(l.date_debut <= l.date_fin for l in lignes)
    # Plusieurs armes sur une même compétition
    assert any(set(l.armes) >= {"EPE", "FLE", "SAB"} for l in lignes)
    # Toutes les lignes ont au moins une catégorie reconnue
    assert sum(1 for l in lignes if not l.categories) == 0


def test_fiche_avec_note():
    fiche = ffe.analyser_fiche(lire("ffe_detail_avec_note.html"))
    assert fiche.titre == "H2036 8eme de finale - 77-91-94"
    assert fiche.lieu == "SAVIGNY-SUR-ORGE"
    assert fiche.date_debut == date(2026, 10, 3)
    assert fiche.armes == ["FLE"]
    assert fiche.categories == ["M15"]
    assert fiche.note_organisation == "https://dirigeant.escrime-ffe.fr/engagement/telecharger/1/107310"
    assert fiche.site_web == "https://escrime-iledefrance.fr/"


def test_fiche_sans_note():
    fiche = ffe.analyser_fiche(lire("ffe_detail_sans_note.html"))
    assert fiche.lieu == "VERSAILLES"
    assert fiche.date_debut == date(2026, 11, 29)
    assert fiche.note_organisation is None


def test_referentiel():
    ref = ffe.analyser_referentiel(lire("ffe_liste_complete.html"))
    assert {a["code"] for a in ref["armes"]} == {"FLE", "EPE", "SAB", "ART", "LAS"}
    assert "M15" in {c["code"] for c in ref["categories"]}
    assert {"code": "11", "libelle": "Régional"} in ref["niveaux"]


def test_parametres_ffe():
    f = ffe.Filtres(armes=["FLE", "EPE"], categories=["M15"], date_debut=date(2026, 11, 1), equipe="equipe")
    p = dict(f.parametres_ffe())
    assert p["arme[FLE]"] == p["arme[EPE]"] == "on"
    assert p["categories"] == "M15"
    assert p["date"] == "01/11/2026"
    assert p["individuelle_equipe[2]"] == "on"
    # Plusieurs catégories : filtrage local, pas côté FFE
    assert dict(ffe.Filtres(categories=["M15", "M17"]).parametres_ffe())["categories"] == ""


# --- CDE 91 -----------------------------------------------------------------------------


def test_cde91_liens_pdf():
    liens = cde91.liens_pdf(lire("cde91_page.html"), "https://cde91.fr/index.php/2015-09-30-10-47-54/2015-09-30-10-48-13")
    assert [d.url for d in liens] == ["https://cde91.fr/images/CalCDE912627V2.pdf"]


def test_cde91_pdf():
    evs = cde91.analyser_pdf((FIXTURES / "cde91_calendrier.pdf").read_bytes(), calendriers.Document("https://cde91.fr/x.pdf"))
    assert len(evs) == 9  # 2 en septembre, 1 en octobre, 4 en novembre, 2 en décembre
    jna = next(e for e in evs if e.titre == "JNA")
    assert jna.date_debut == date(2026, 9, 26)
    assert jna.lieu == "CHILLY MAZARIN"
    assert jna.armes == ["FLE", "EPE", "SAB"]
    assert jna.categories[0] == "M15" and "V4" in jna.categories
    assert jna.horaire == "A définir"
    cr = [e for e in evs if e.titre == "Challenge de Référence 1"]
    assert len(cr) == 2
    cf = [e for e in evs if e.titre == "Challenge de France – épreuve départementale"]
    assert {e.armes[0] for e in cf} == {"FLE", "EPE"}
    edj = next(e for e in evs if e.titre == "Entraînement des Jeunes 1")
    assert edj.date_debut == date(2026, 11, 21)
    assert edj.horaire == "13h30 15h45"
    stages = [e for e in evs if e.titre.startswith("STAGE")]
    assert {e.lieu for e in stages} == {"SAVIGNY", "MASSY"}
    assert all(e.date_debut == date(2026, 12, 12) for e in stages)
    assert len({e.id for e in evs}) == len(evs)


def test_meme_evenement():
    j = date(2026, 10, 4)
    e = calendriers.Evenement("x", "CR 1", "SAVIGNY S/ ORGE", j, j, ["FLE"], ["M9", "M11"], "M9", None, "u")
    assert calendriers.meme_evenement(e, j, j, "SAVIGNY-SUR-ORGE", ["FLE"], ["M11", "M13"])
    assert not calendriers.meme_evenement(e, j, j, "SAVIGNY-SUR-ORGE", ["EPE"], [])
    assert not calendriers.meme_evenement(e, date(2026, 10, 5), date(2026, 10, 5), "SAVIGNY-SUR-ORGE", ["FLE"], [])
    assert not calendriers.meme_evenement(e, j, j, "SAVIGNY-SUR-ORGE", ["FLE"], ["M20"])  # catégories disjointes
    # Calendrier de la Ligue : le week-end entier ; la FFE donne le jour exact
    w = calendriers.Evenement("y", "IDF 1", "St Gratien", date(2026, 9, 26), date(2026, 9, 27), ["EPE"], ["M20"], "M20", None, "u")
    assert calendriers.meme_evenement(w, date(2026, 9, 27), date(2026, 9, 27), "SAINT-GRATIEN", ["EPE"], ["M20"])
    b = calendriers.Evenement("z", "IDF 1", "Beaumont/Oise", date(2026, 10, 10), date(2026, 10, 11), ["EPE"], [], "", None, "u")
    assert calendriers.meme_evenement(b, date(2026, 10, 11), date(2026, 10, 11), "BEAUMONT-SUR-OISE", ["EPE"], [])


# --- ICS --------------------------------------------------------------------------------


def test_ics():
    c = Competition(
        id="ffe-abc", sources=["ffe", "cde91"], titre="Tournoi, départemental", lieu="SAVIGNY-SUR-ORGE",
        date_debut=date(2026, 10, 4), date_fin=date(2026, 10, 4), armes=["FLE"], categories=["M9"],
        categories_libelle="M9", horaire="13h30", url="https://ffe/x", note_url="https://api/competitions/ffe-abc/note",
    )
    texte = ics.generer([c], "Test", "calendrier-ffe.benribs.fr")
    assert texte.startswith("BEGIN:VCALENDAR\r\n")
    assert "DTSTART;VALUE=DATE:20261004" in texte
    assert "DTEND;VALUE=DATE:20261005" in texte
    assert "SUMMARY:Tournoi\\, départemental – Fleuret (M9)" in texte
    assert "UID:ffe-abc@calendrier-ffe.benribs.fr" in texte
    assert all(len(l.encode()) <= 75 for l in texte.split("\r\n"))


# --- Géolocalisation --------------------------------------------------------------------


def test_choix_commune():
    from app.geo import Geolocalisation

    savigny_91 = [{"nom": "Savigny-sur-Orge", "codeDepartement": "91", "codeRegion": "11"}]
    assert Geolocalisation._choisir("SAVIGNY", savigny_91) is None  # national : nom partiel refusé
    assert Geolocalisation._choisir("SAVIGNY", savigny_91, partiel=True) == savigny_91[0]
    paris = [{"nom": "Paris", "codeDepartement": "75", "codeRegion": "11"}]
    assert Geolocalisation._choisir("PARIS13", paris) == paris[0]


# --- Type / échelon FFE -----------------------------------------------------------------


def test_type_et_echelon():
    import base64

    def jeton(brut):
        return base64.b64encode(brut.encode()).decode()

    assert ffe.type_et_echelon(jeton("2-separator-5-separator-1-separator-101026-separator-101026-separator-ETAMPES")) == ("epreuve", "national")
    assert ffe.type_et_echelon(jeton("3-separator-5-separator---separator-010527-separator-020527-separator-X")) == ("championnat", "national")
    assert ffe.type_et_echelon(jeton("1-separator---separator---separator-041026-separator-041026-separator-SAVIGNY")) == ("tournoi", None)
    assert ffe.type_et_echelon("pas du base64 !") == (None, None)


def test_liste_types():
    lignes = ffe.analyser_liste(lire("ffe_liste_complete.html"))
    nationales = [l for l in lignes if (l.type, l.echelon) == ("epreuve", "national")]
    assert nationales and all("National" in l.titre for l in nationales)
    championnats_france = [l for l in lignes if (l.type, l.echelon) == ("championnat", "national")]
    assert championnats_france and all("Championnat National" in l.titre for l in championnats_france)



def test_departements_du_titre():
    from app.service import departements_du_titre

    assert departements_du_titre("H2036 8eme de finale - 77-91-94") == ["77", "91", "94"]
    assert departements_du_titre("H2036 8eme de finale - 75-93") == ["75", "93"]
    assert departements_du_titre("H2036 Quart de finale") == []
    assert departements_du_titre("Epreuve Régional(e) 1") == []
    assert departements_du_titre("Tournoi 2026") == []


def test_titre_complet():
    assert cde91.titre_complet("CR 1") == "Challenge de Référence 1"
    assert cde91.titre_complet("CR") == "Challenge de Référence"
    assert cde91.titre_complet("CF DEPARTEMENTAL") == "Challenge de France – épreuve départementale"
    assert cde91.titre_complet("CF") == "Challenge de France"
    assert cde91.titre_complet("EDJ 1") == "Entraînement des Jeunes 1"
    assert cde91.titre_complet("CRITERIUM") == "CRITERIUM"
    assert cde91.titre_complet("JNA") == "JNA"


# --- Ligue d'Île-de-France ----------------------------------------------------------------


def test_idf_liens():
    docs = idf.liens_calendriers(lire("idf_page.html"), "https://escrime-iledefrance.fr/liste-des-competitions/")
    # Un calendrier par lien et par arme (même si les liens pointent aujourd'hui vers le même fichier).
    # Les calendriers M13 n'ont pas encore de lien : ignorés. Les règles de classement : ignorées.
    assert [d.libelle for d in docs] == ["Calendrier IDF Epée 26-27", "Calendrier IDF Fleuret 26-27", "Calendrier IDF Sabre 26-27"]
    assert all(d.url.endswith("Calendrier-IDF-2026-2027-version-25.09.26.xlsx.pdf") for d in docs)


@pytest.mark.parametrize(
    "texte, attendu",
    [
        ("IDF 1 - St Gratien", ("IDF 1", "St Gratien", [], None)),
        ("1/8 Finale FDJ - 78-92-95", ("1/8 Finale FDJ", "", ["78", "92", "95"], None)),
        ("1/8 finale FDJ - Savigny/Orge (77-91-94)", ("1/8 finale FDJ", "Savigny/Orge", ["77", "91", "94"], None)),
        ("1/8 Finale FDJ - 3 lieux - Bobigny - Vaureal", ("1/8 Finale FDJ", "Bobigny, Vaureal", [], None)),
        ("Challenge IDF Individuel et équipe-Val d'Orge", ("Challenge IDF Individuel et équipe", "Val d'Orge", [], None)),
        ("EN 2 + EQ - Maisons-Alfort", ("EN 2 + EQ", "Maisons-Alfort", [], None)),
        ("Vincennes - 09/01", ("Vincennes", "Vincennes", [], (9, 1))),
        ("IDF 1 -", ("IDF 1", "", [], None)),
        ("JNA", ("JNA", "", [], None)),
        ("Colmar", ("Colmar", "Colmar", [], None)),
        ("Hérouville + EQ", ("Hérouville + EQ", "Hérouville", [], None)),
        ("FRANCE - N2/N3 Castelnau le Lez", ("FRANCE N2/N3", "Castelnau le Lez", [], None)),
        ("FRANCE Rennes", ("FRANCE", "Rennes", [], None)),
        ("Chpt IDF Individuel - Melun", ("Chpt IDF Individuel", "Melun", [], None)),
    ],
)
def test_idf_decouper(texte, attendu):
    assert idf.decouper(texte) == attendu


def test_idf_nettoyer_case():
    assert idf.nettoyer_case("V A") == ""
    assert idf.nettoyer_case("N C\nEN 4 - Lisieux") == "EN 4 - Lisieux"
    assert idf.nettoyer_case("Chpt IDF Individuel -\nBobigny") == "Chpt IDF Individuel - Bobigny"
    assert idf.nettoyer_case("EN 2 + EQ - Maisons-\nAlfort") == "EN 2 + EQ - Maisons-Alfort"


def _calendrier(libelle):
    contenu = (FIXTURES / "idf_calendrier.pdf").read_bytes()
    return idf.analyser_pdf(contenu, calendriers.Document("https://idf/cal.pdf", libelle))


def _trouver(evs, titre, lieu=None):
    return [e for e in evs if e.titre == titre and (lieu is None or e.lieu == lieu)]


def test_idf_une_arme_par_lien():
    epee, fleuret, sabre = (_calendrier(f"Calendrier IDF {a} 26-27") for a in ("Epée", "Fleuret", "Sabre"))
    assert {a for e in epee for a in e.armes} == {"EPE"}
    assert {a for e in fleuret for a in e.armes} == {"FLE"}
    assert {a for e in sabre for a in e.armes} == {"SAB"}
    assert len(epee) > 40 and len(fleuret) > 40 and len(sabre) > 35
    # Le fleuret n'a pas de « EN 1 - Rodez » (c'est l'épée) ; l'épée n'a pas « IDF 1 - Mennecy » (c'est le fleuret)
    assert not _trouver(fleuret, "Épreuve nationale 1", "Rodez") and _trouver(epee, "Épreuve nationale 1", "Rodez")
    assert not _trouver(epee, "IDF 1", "Mennecy") and _trouver(fleuret, "IDF 1", "Mennecy")


def test_idf_m13():
    # Les compétitions M13 des calendriers fleuret et épée sont prises en compte
    fleuret = _calendrier("Calendrier IDF Fleuret 26-27")
    m13 = [e for e in fleuret if "M13" in e.categories]
    assert _trouver(m13, "Challenge de France", "Angoulême")
    assert _trouver(m13, "Challenge IDF Individuel et équipe", "Val d'Orge")
    # Calendrier « M13 » dédié : uniquement la colonne M13
    dedie = _calendrier("Calendrier IDF M13 Fleuret 26-27")
    assert dedie and all(e.categories == ["M13"] and e.armes == ["FLE"] for e in dedie)


def test_idf_grille():
    epee = _calendrier("Calendrier IDF Epée 26-27")
    fleuret = _calendrier("Calendrier IDF Fleuret 26-27")
    assert len({e.id for e in epee}) == len(epee)
    # Week-end du 26-27 septembre, épée M20 : « JNA / IDF 1 - St Gratien » = deux événements
    idf1 = _trouver(epee, "IDF 1", "St Gratien")
    assert (idf1[0].date_debut, idf1[0].date_fin, idf1[0].categories) == (date(2026, 9, 26), date(2026, 9, 27), ["M20"])
    assert any(e.titre == "JNA" and e.date_debut == date(2026, 9, 26) for e in epee)
    # Plusieurs colonnes, une seule épreuve ; « 17-18-19 » sur trois jours
    rodez = _trouver(epee, "Épreuve nationale 1", "Rodez")[0]
    assert (rodez.date_debut, rodez.date_fin) == (date(2026, 10, 17), date(2026, 10, 19))
    assert rodez.categories == ["M17", "M20", "SENIOR"]
    # « 31-1 » : à cheval sur deux mois
    toulouse = _trouver(epee, "Toulouse")[0]
    assert (toulouse.date_debut, toulouse.date_fin) == (date(2026, 10, 31), date(2026, 11, 1))
    # Texte sous l'incrustation VACANCES
    assert _trouver(epee, "Épreuve nationale 4", "Lisieux")[0].date_debut == date(2027, 2, 13)
    # Date précise dans la case
    assert {e.date_debut for e in _trouver(epee, "Vincennes")} == {date(2027, 1, 9), date(2027, 1, 10)}
    # Texte qui déborde sur les cases voisines : rattaché à la colonne où il est centré
    decembre = [e for e in fleuret if e.date_debut == date(2026, 12, 5)]
    assert {(e.titre, e.lieu, tuple(e.categories)) for e in decembre} >= {
        ("Cabries", "Cabries", ("M17",)),
        ("1/8 Finale Fête des Jeunes", "Bobigny, Vaureal", ("M15",)),
    }
    assert not any("M13" in e.categories for e in decembre)
    # Lieu non précisé
    assert _trouver(fleuret, "IDF 1", "")
    # Départements en fin de titre
    assert _trouver(fleuret, "1/8 finale Fête des Jeunes (77-91-94)", "Savigny/Orge")
    assert _trouver(fleuret, "1/8 Finale Fête des Jeunes (78-92-95)")
    # Abréviations développées, aucune trace de VACANCES
    assert _trouver(fleuret, "Championnat de France", "Chinon")
    sabre = _calendrier("Calendrier IDF Sabre 26-27")
    assert _trouver(sabre, "Entraînement des Jeunes 1 Individuel", "Marly le Roi")[0].categories == ["M9", "M11", "M13"]
    assert _trouver(sabre, "Championnat de France", "Rennes")
    assert not any(" V A" in e.titre or "C A " in e.titre for e in epee + fleuret + sabre)


def test_eclater_par_arme():
    from app.models import Competition
    from app.service import eclater, separer_id

    c = Competition(id="ffe-abc", sources=["ffe"], titre="T", lieu="L", date_debut=date(2026, 10, 3),
                    date_fin=date(2026, 10, 3), armes=["EPE", "FLE"], categories=["M15"])
    epee, fleuret = eclater(c)
    assert (epee.id, epee.armes, fleuret.id, fleuret.armes) == ("ffe-abc.EPE", ["EPE"], "ffe-abc.FLE", ["FLE"])
    assert separer_id("ffe-abc.FLE") == ("ffe-abc", "FLE")
    assert separer_id("ffe-abc") == ("ffe-abc", None)
    assert separer_id("ffe-ab_c~~") == ("ffe-ab_c~~", None)


def _page_seule(numero):
    """Fichier d'une seule page du calendrier commun (fichier séparé par arme)."""
    import io
    import pypdfium2 as pdfium
    source = pdfium.PdfDocument((FIXTURES / "idf_calendrier.pdf").read_bytes())
    nouveau = pdfium.PdfDocument.new()
    nouveau.import_pages(source, [numero])
    sortie = io.BytesIO()
    nouveau.save(sortie)
    return sortie.getvalue()


def test_idf_fichier_commun_ou_separe():
    # Fichier commun (3 pages : épée, fleuret, sabre) et fichiers séparés : mêmes compétitions pour chaque arme
    for numero, arme, libelle in ((0, "EPE", "Epée"), (1, "FLE", "Fleuret"), (2, "SAB", "Sabre")):
        commun = _calendrier(f"Calendrier IDF {libelle} 26-27")
        separe = idf.analyser_pdf(_page_seule(numero), calendriers.Document("https://idf/x.pdf", f"Calendrier IDF {libelle} 26-27"))
        assert [(e.id, e.categories) for e in separe] == [(e.id, e.categories) for e in commun]
        assert {a for e in separe for a in e.armes} == {arme}


def test_idf_lien_vers_une_autre_arme_ignore():
    # Lien « Fleuret » vers un fichier qui ne contient que l'épée : ignoré, jamais d'épée rangée en fleuret
    with pytest.raises(calendriers.DocumentIgnore, match="Fleuret.*Épée"):
        idf.analyser_pdf(_page_seule(0), calendriers.Document("https://idf/x.pdf", "Calendrier IDF Fleuret 26-27"))


@pytest.mark.parametrize("titre,attendu", [
    ("NE PAS UTILISER", True),
    ("Ne pas utiliser - test", True),
    ("Épreuve à ne  pas utiliser", True),
    ("Tournoi de Noël", False),
    ("UTILISER le gymnase", False),
])
def test_titre_a_ignorer(titre, attendu):
    from app.service import a_ignorer
    assert a_ignorer(titre) is attendu
