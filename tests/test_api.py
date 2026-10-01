"""Tests des routes, avec un faux réseau (httpx.MockTransport) servant les pages enregistrées."""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import creer_app
from app.service import departements_du_titre
from app.text import cle_ville

JETON_TEST = "jeton-de-test-suffisamment-long"
FIXTURES = Path(__file__).parent / "fixtures"

# Clé normalisée (cle_ville) -> (nom, département, région, latitude, longitude)
COMMUNES = {
    "SAVIGNYSURORGE": ("Savigny-sur-Orge", "91", "11", 48.6851, 2.3493),
    "SAVIGNY": ("Savigny-sur-Orge", "91", "11", 48.6851, 2.3493),
    "MENNECY": ("Mennecy", "91", "11", 48.5660, 2.4370),
    "CHILLYMAZARIN": ("Chilly-Mazarin", "91", "11", 48.7020, 2.3120),
    "ETAMPES": ("Étampes", "91", "11", 48.4350, 2.1610),
    "MASSY": ("Massy", "91", "11", 48.7300, 2.2760),
}


def reseau(appels: list[str]):
    def repondre(requete: httpx.Request) -> httpx.Response:
        url = requete.url
        appels.append(str(url))
        if url.host == "www.ffescrime.fr" and url.path == "/calendrier/":
            nom = "ffe_liste_savigny.html" if url.params.get("lieu_ville") else "ffe_liste_complete.html"
            return httpx.Response(200, text=(FIXTURES / nom).read_text(encoding="utf-8"))
        if url.host == "www.ffescrime.fr" and url.path.startswith("/competition/"):
            return httpx.Response(200, text=(FIXTURES / "ffe_detail_avec_note.html").read_text(encoding="utf-8"))
        if url.host == "cde91.fr" and url.path.endswith(".pdf"):
            return httpx.Response(200, content=(FIXTURES / "cde91_calendrier.pdf").read_bytes())
        if url.host == "cde91.fr":
            return httpx.Response(200, text=(FIXTURES / "cde91_page.html").read_text(encoding="utf-8"))
        if url.host == "exemple.fr" and url.path.endswith(".pdf"):
            return httpx.Response(200, content=(FIXTURES / "idf_calendrier.pdf").read_bytes())
        if url.host == "exemple.fr":
            return httpx.Response(200, text="<html>pas un pdf</html>")
        if url.host == "escrime-iledefrance.fr" and url.path.endswith(".pdf"):
            return httpx.Response(200, content=(FIXTURES / "idf_calendrier.pdf").read_bytes())
        if url.host == "escrime-iledefrance.fr":
            return httpx.Response(200, text=(FIXTURES / "idf_page.html").read_text(encoding="utf-8"))
        if url.host == "geo.api.gouv.fr" and url.path == "/regions":
            return httpx.Response(200, json=[{"nom": "Île-de-France", "code": "11"}])
        if url.host == "geo.api.gouv.fr" and url.path == "/departements":
            return httpx.Response(200, json=[{"nom": "Essonne", "code": "91", "codeRegion": "11"}])
        if url.host == "geo.api.gouv.fr":
            c = COMMUNES.get(cle_ville(url.params["nom"]))
            return httpx.Response(200, json=[{
                "nom": c[0], "codeDepartement": c[1], "codeRegion": c[2],
                "centre": {"type": "Point", "coordinates": [c[4], c[3]]},
            }] if c else [])
        return httpx.Response(404)

    return httpx.MockTransport(repondre)


@pytest.fixture
def client(tmp_path):
    appels: list[str] = []
    settings = Settings(data_dir=tmp_path, admin_token=JETON_TEST, public_url="https://api.test", _env_file=None)
    app = creer_app(settings, demarrer_taches=False, transport=reseau(appels))
    with TestClient(app) as c:
        for source in ("cde91", "idf"):
            c.post(f"/calendriers/{source}/refresh", headers={"Authorization": "Bearer " + JETON_TEST})
        c.appels = appels
        yield c


def test_liste_fusion_ffe_cde(client):
    r = client.get("/competitions", params={"ville": "savigny", "arme": "FLE"})
    assert r.status_code == 200
    d = r.json()
    titres = {(c["date_debut"], c["titre"]): c for c in d["competitions"]}
    # Le 'Tournoi' FFE du 4/10 et le 'CR 1' du CDE sont la même compétition : le nom du CDE l'emporte
    tournoi = titres[("2026-10-04", "Challenge de Référence 1")]
    assert tournoi["sources"] == ["ffe", "cde91"]
    assert tournoi["id"].startswith("ffe-")
    assert tournoi["horaire"] == "Cf note club"
    assert ("2026-10-04", "Tournoi") not in titres
    assert tournoi["note_url"].startswith("https://api.test/competitions/ffe-")
    # Le stage CDE à 'SAVIGNY' reste présent
    assert ("2026-12-12", "STAGE DEPARTEMENTAL 1") in titres
    assert d["sources"]["ffe"]["ok"] and d["sources"]["cde91"]["ok"]


def test_filtre_departement(client):
    d = client.get("/competitions", params={"departement": "91"}).json()
    assert d["count"] > 0
    assert all(c["departement"] == "91" for c in d["competitions"])


def test_cache_ffe(client):
    client.get("/competitions", params={"arme": "EPE", "source": "ffe"})
    client.get("/competitions", params={"arme": "EPE", "source": "ffe"})
    assert sum("ffescrime.fr/calendrier" in a for a in client.appels) == 1


def test_detail_et_note(client):
    id_ = client.get("/competitions", params={"ville": "savigny", "source": "ffe"}).json()["competitions"][0]["id"]
    d = client.get(f"/competitions/{id_}").json()
    assert d["note_organisation"] == "https://dirigeant.escrime-ffe.fr/engagement/telecharger/1/107310"
    r = client.get(f"/competitions/{id_}/note", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == d["note_organisation"]


def test_ics(client):
    r = client.get("/competitions.ics", params={"departement": "91", "nom": "Club"})
    assert r.headers["content-type"].startswith("text/calendar")
    assert "X-WR-CALNAME:Club" in r.text
    assert r.text.count("BEGIN:VEVENT") > 0


def test_validation_et_securite(client):
    assert client.get("/competitions", params={"arme": "XYZ"}).status_code == 422
    assert client.get("/competitions", params={"source": "autre"}).status_code == 422
    assert client.post("/calendriers/cde91/refresh").status_code == 401
    assert client.get("/calendriers/cde91").json()["nb_evenements"] == 9
    assert client.get("/calendriers/idf").json()["nb_evenements"] > 100
    assert client.get("/calendriers/inconnu").status_code == 404


def test_referentiel(client):
    ref = client.get("/referentiel").json()
    assert {"armes", "categories", "niveaux", "regions", "departements", "sources"} <= set(ref)
    assert ref["departements"] == [{"code": "91", "libelle": "91 – Essonne", "region": "11"}]


def test_officielle(client):
    toutes = client.get("/competitions").json()["competitions"]
    officielles = client.get("/competitions", params={"officielle": "true"}).json()["competitions"]
    assert 0 < len(officielles) < len(toutes)
    assert all(c["officielle"] for c in officielles)
    par_titre = {(c["date_debut"], c["titre"]): c for c in toutes}
    # Tout le CDE 91, y compris le Tournoi FFE fusionné avec le CR 1 du CDE
    assert all(c["officielle"] for c in toutes if "cde91" in c["sources"])
    assert par_titre[("2026-10-04", "Challenge de Référence 1")]["officielle"]
    # H2036 en Île-de-France, seulement si le 91 est concerné
    assert par_titre[("2026-10-03", "H2036 8eme de finale - 77-91-94")]["officielle"]
    # Circuits nationaux partout (villes non géolocalisées dans ce test)
    assert all(c["officielle"] for c in toutes if (c["type"], c["echelon"]) == ("epreuve", "national"))
    # Un tournoi hors CDE et hors national n'est pas officiel
    assert not any(c["officielle"] for c in toutes if c["type"] == "tournoi" and c["sources"] == ["ffe"])
    # Aucune compétition départementale FFE hors calendrier du CDE
    assert not any(c["officielle"] for c in toutes if c["echelon"] == "departemental" and c["sources"] == ["ffe"])


def test_reanalyse_si_nouvelle_version(tmp_path):
    """Un état enregistré par une ancienne version de l'analyse est ré-analysé dès le démarrage."""
    import json

    from app import cde91

    dossier = tmp_path / "cde91"
    (dossier / "pdf").mkdir(parents=True)
    (dossier / "pdf" / "abc_cal.pdf").write_bytes((FIXTURES / "cde91_calendrier.pdf").read_bytes())
    ancien = {
        "pdfs": [{"url": "https://cde91.fr/cal.pdf", "sha256": "x", "fichier": "abc_cal.pdf"}],
        "evenements": [{"id": "vieux", "titre": "CF DEPARTEMENTAL", "lieu": "ETAMPES", "date_debut": "2026-11-21",
                        "date_fin": "2026-11-21", "armes": ["FLE"], "categories": ["M13"], "categories_libelle": "M13",
                        "horaire": None, "pdf_url": "https://cde91.fr/cal.pdf"}],
        "verifie_le": None, "erreur": None,
    }
    (dossier / "etat.json").write_text(json.dumps(ancien), encoding="utf-8")
    from app.calendriers import CalendrierPDF

    cal = CalendrierPDF(
        cde91.NOM, cde91.LIBELLE, None, "https://cde91.fr/page", tmp_path,
        cde91.liens_pdf, cde91.analyser_pdf, cde91.VERSION_ANALYSE,
    )
    titres = {e.titre for e in cal.etat.evenements}
    assert cal.etat.version == cde91.VERSION_ANALYSE
    assert "CF DEPARTEMENTAL" not in titres
    assert "Challenge de France – épreuve départementale" in titres


def test_une_competition_par_arme(client):
    toutes = client.get("/competitions", params={"source": "ffe"}).json()["competitions"]
    assert all(len(c["armes"]) == 1 for c in toutes)
    # Le faux réseau renvoie toujours la même fiche détail (fleuret) : on prend la part fleuret d'une fiche multi-armes
    multi = [c for c in toutes if c["id"].endswith(".FLE")]
    assert multi
    # Le détail et la note fonctionnent avec l'identifiant suffixé
    d = client.get(f"/competitions/{multi[0]['id']}").json()
    assert d["armes"] == multi[0]["armes"]
    assert client.get(f"/competitions/{multi[0]['id']}/note", follow_redirects=False).status_code == 302


def test_lieu_indetermine(client):
    idf_seul = client.get("/competitions", params={"source": "idf"}).json()["competitions"]
    sans_lieu = [c for c in idf_seul if c["lieu"] == "Lieu indéterminé"]
    assert sans_lieu and all(c["region"] == "11" for c in sans_lieu)


def test_ligue_idf(client):
    d = client.get("/competitions", params={"source": "idf,ffe", "arme": "EPE"}).json()
    assert d["sources"]["idf"]["ok"]
    idf_seul = [c for c in d["competitions"] if c["sources"] == ["idf"]]
    assert idf_seul and all(c["officielle"] for c in idf_seul if "(" not in c["titre"])
    # Une compétition présente à la FFE et à la Ligue est fusionnée (dates exactes FFE)
    fusion = [c for c in d["competitions"] if set(c["sources"]) == {"ffe", "idf"}]
    assert fusion and all(c["id"].startswith("ffe-") for c in fusion)
    assert all(c["officielle"] for c in fusion if not departements_du_titre(c["titre"]))
    # Réservée à d'autres départements : pas officielle
    fleuret = client.get("/competitions", params={"source": "idf", "arme": "FLE"}).json()["competitions"]
    exclues = [c for c in fleuret if "(78-92-95)" in c["titre"]]
    assert exclues and not any(c["officielle"] for c in exclues)
    assert all(c["officielle"] for c in fleuret if "(77-91-94)" in c["titre"])


# --- Remplacement manuel des calendriers -----------------------------------------------------

JETON = {"Authorization": "Bearer " + JETON_TEST}


def _evenements(client, arme):
    return [c for c in client.get("/competitions", params={"source": "idf", "arme": arme}).json()["competitions"]]


def _cible(client, source, code):
    return next(c for c in client.get(f"/calendriers/{source}").json()["cibles"] if c["cible"] == code)


def test_cibles(client):
    assert [c["cible"] for c in client.get("/calendriers/idf").json()["cibles"]] == ["fleuret", "epee", "sabre"]
    assert [c["cible"] for c in client.get("/calendriers/cde91").json()["cibles"]] == ["cde91"]
    assert _cible(client, "idf", "fleuret")["mode"] == "site"


def test_remplacer_par_fichier(client):
    epee_avant = {(c["id"], c["url"]) for c in _evenements(client, "EPE")}
    pdf = (FIXTURES / "idf_calendrier.pdf").read_bytes()
    r = client.post("/calendriers/idf/cibles/fleuret", headers=JETON,
                    files={"fichier": ("Fleuret IDF 26-27.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    assert r.json()["nb_evenements"] > 40
    cible = _cible(client, "idf", "fleuret")
    assert cible["mode"] == "fichier" and cible["calendriers"][0]["nom"] == "Fleuret IDF 26-27.pdf"
    # Les compétitions fleuret renvoient vers le fichier déposé, servi par l'API
    urls = {c["url"] for c in _evenements(client, "FLE")}
    assert len(urls) == 1 and urls.pop().startswith("https://api.test/calendriers/idf/fichiers/")
    fichier = cible["calendriers"][0]["url"].rsplit("/", 1)[1]
    servi = client.get(f"/calendriers/idf/fichiers/{fichier}")
    assert servi.status_code == 200 and servi.headers["content-type"] == "application/pdf" and servi.content == pdf
    # L'épée n'a pas bougé
    assert {(c["id"], c["url"]) for c in _evenements(client, "EPE")} == epee_avant
    assert _cible(client, "idf", "epee")["mode"] == "site"


def test_remplacer_par_lien_et_persistance(client):
    lien = "https://exemple.fr/calendriers/sabre.pdf"
    r = client.post("/calendriers/idf/cibles/sabre", headers=JETON, data={"lien": lien})
    assert r.status_code == 200, r.text
    assert _cible(client, "idf", "sabre")["mode"] == "lien"
    assert {c["url"] for c in _evenements(client, "SAB")} == {lien}
    # L'analyse quotidienne ne l'écrase pas
    client.post("/calendriers/idf/refresh", headers=JETON)
    assert {c["url"] for c in _evenements(client, "SAB")} == {lien}
    assert _cible(client, "idf", "sabre")["mode"] == "lien"
    # Le lien du calendrier est aussi donné sur une compétition fusionnée avec la FFE
    toutes = client.get("/competitions", params={"arme": "SAB"}).json()["competitions"]
    fusion = [c for c in toutes if set(c["sources"]) >= {"ffe", "idf"}]
    assert fusion and all({"source": "idf", "libelle": "Calendrier Ligue IDF – Sabre", "url": lien} in c["calendriers"] for c in fusion)
    # Retour au calendrier du site
    r = client.delete("/calendriers/idf/cibles/sabre", headers=JETON)
    assert r.status_code == 200
    assert _cible(client, "idf", "sabre")["mode"] == "site"
    assert lien not in {c["url"] for c in _evenements(client, "SAB")}


def test_remplacer_refuse_sans_rien_changer(client):
    avant = client.get("/calendriers/idf").json()
    # Le calendrier du CDE ne contient pas de page « Fleuret » de la Ligue : refusé
    r = client.post("/calendriers/idf/cibles/fleuret", headers=JETON,
                    files={"fichier": ("cde.pdf", (FIXTURES / "cde91_calendrier.pdf").read_bytes(), "application/pdf")})
    assert r.status_code == 422 and "Aucune compétition" in r.json()["detail"]
    # Pas un PDF, lien qui ne renvoie pas un PDF, rien de fourni, jeton absent, cible inconnue
    assert client.post("/calendriers/idf/cibles/fleuret", headers=JETON,
                       files={"fichier": ("x.pdf", b"bonjour", "application/pdf")}).status_code == 422
    assert client.post("/calendriers/idf/cibles/fleuret", headers=JETON, data={"lien": "https://exemple.fr/page"}).status_code == 422
    assert client.post("/calendriers/idf/cibles/fleuret", headers=JETON).status_code == 422
    assert client.post("/calendriers/idf/cibles/fleuret", data={"lien": "https://exemple.fr/a.pdf"}).status_code == 401
    assert client.post("/calendriers/idf/cibles/fleurette", headers=JETON, data={"lien": "https://exemple.fr/a.pdf"}).status_code == 404
    apres = client.get("/calendriers/idf").json()
    assert apres["cibles"] == avant["cibles"] and apres["nb_evenements"] == avant["nb_evenements"]


def test_remplacer_cde91(client):
    pdf = (FIXTURES / "cde91_calendrier.pdf").read_bytes()
    r = client.post("/calendriers/cde91/cibles/cde91", headers=JETON,
                    files={"fichier": ("Calendrier CDE 91 v3.pdf", pdf, "application/pdf")})
    assert r.status_code == 200 and r.json()["nb_evenements"] == 9
    cde = client.get("/competitions", params={"source": "cde91"}).json()["competitions"]
    assert all(c["calendriers"][0]["url"].startswith("https://api.test/calendriers/cde91/fichiers/") for c in cde)
    assert client.get("/calendriers/cde91/fichiers/..%2Fetat.json").status_code == 404


def test_adresse_des_fichiers_suit_cal_public_url(tmp_path):
    """Si CAL_PUBLIC_URL change, les liens des fichiers déposés sont corrigés au redémarrage."""
    appels: list[str] = []

    def demarrer(public_url):
        s = Settings(data_dir=tmp_path, admin_token=JETON_TEST, public_url=public_url, _env_file=None)
        return TestClient(creer_app(s, demarrer_taches=False, transport=reseau(appels)))

    pdf = (FIXTURES / "idf_calendrier.pdf").read_bytes()
    with demarrer("https://mauvaise.adresse") as c:
        c.post("/calendriers/idf/refresh", headers=JETON)
        assert c.post("/calendriers/idf/cibles/fleuret", headers=JETON,
                      files={"fichier": ("f.pdf", pdf, "application/pdf")}).status_code == 200
    with demarrer("https://api.exemple.fr") as c:
        urls = {c["url"] for c in _evenements(c, "FLE")}
        assert len(urls) == 1 and urls.pop().startswith("https://api.exemple.fr/calendriers/idf/fichiers/")
        cible = _cible(c, "idf", "fleuret")
        assert cible["calendriers"][0]["url"].startswith("https://api.exemple.fr/")



# --- Filtre de distance ------------------------------------------------------------------


def test_distance_depuis_une_commune(client):
    d = client.get("/competitions", params={"pres_de": "savigny sur orge", "rayon": 10}).json()
    assert d["depart"]["nom"] == "Savigny-sur-Orge" and d["depart"]["rayon_km"] == 10
    assert d["count"] > 0
    assert all(c["distance_km"] is not None and c["distance_km"] <= 10 for c in d["competitions"])
    lieux = {c["lieu"] for c in d["competitions"]}
    assert "SAVIGNY-SUR-ORGE" in lieux and not any("ETAMPES" in l.upper() for l in lieux)  # Étampes ~30 km
    assert d["sans_position"] > 0  # lieux inconnus (ex. villes non géolocalisées dans ce test) : écartés et comptés
    # Rayon plus large : Étampes apparaît
    large = client.get("/competitions", params={"pres_de": "Savigny-sur-Orge", "rayon": 40}).json()
    assert any("ETAMPES" in c["lieu"].upper() for c in large["competitions"])


def test_distance_depuis_une_position(client):
    d = client.get("/competitions", params={"lat": 48.685, "lon": 2.349, "rayon": 2, "source": "cde91"}).json()  # Chilly-Mazarin : ~3 km
    assert d["depart"]["nom"] is None
    assert {c["lieu"] for c in d["competitions"]} == {"SAVIGNY S/ ORGE", "SAVIGNY"}
    # Sans rayon : la distance est donnée, rien n'est filtré
    tout = client.get("/competitions", params={"lat": 48.685, "lon": 2.349, "source": "cde91"}).json()
    assert tout["count"] == 11 and all(c["distance_km"] is not None for c in tout["competitions"])  # 9 lignes, JNA = 3 armes
    # Le flux .ics accepte le même filtre
    ics = client.get("/competitions.ics", params={"lat": 48.685, "lon": 2.349, "rayon": 2, "source": "cde91"})
    assert ics.text.count("BEGIN:VEVENT") == 2


def test_distance_erreurs(client):
    assert client.get("/competitions", params={"rayon": 10}).status_code == 422
    assert client.get("/competitions", params={"pres_de": "Nullepart-sur-Rien", "rayon": 10}).status_code == 422
    assert client.get("/competitions", params={"lat": 48.6, "rayon": 10}).status_code == 422
    assert client.get("/competitions", params={"lat": 120, "lon": 2, "rayon": 10}).status_code == 422


# --- Compétitions ajoutées à la main -----------------------------------------------------

ADMIN = {"Authorization": "Bearer " + JETON_TEST}


def _manuelle(**champs):
    return {"titre": "Stage de Toussaint", "lieu": "Massy", "date_debut": "2026-10-26", "categories": ["M11", "M13"], **champs}


def test_manuelle_ajout_liste_detail(client):
    assert client.post("/manuelles", json=_manuelle()).status_code == 401
    r = client.post("/manuelles", json=_manuelle(
        armes=["EPE"], remarque="Gratuit", document_url="https://exemple.fr/stage.pdf", document_nom="Programme",
        preinscription=False, inscription_sur_place=True,
    ), headers=ADMIN)
    assert r.status_code == 201, r.text
    m = r.json()
    assert m["id"].startswith("manuel-2026-10-26-stage-de-toussaint-massy-") and m["date_fin"] == "2026-10-26"

    d = client.get("/competitions", params={"source": "manuel", "arme": "EPE", "officielle": "true"}).json()
    assert d["count"] == 1
    c = d["competitions"][0]
    assert c["sources"] == ["manuel"] and c["officielle"] and c["departement"] == "91"
    assert c["remarque"] == "Gratuit" and c["preinscription"] is False and c["inscription_sur_place"] is True
    assert c["calendriers"] == [{"source": "manuel", "libelle": "Programme", "url": "https://exemple.fr/stage.pdf"}]
    assert c["categories_libelle"] == "M11, M13"
    assert client.get(f"/competitions/{m['id']}").json()["titre"] == "Stage de Toussaint"
    assert "Remarque : Gratuit" in client.get("/competitions.ics", params={"source": "manuel"}).text

    # Modification, puis suppression
    r = client.put(f"/manuelles/{m['id']}", json=_manuelle(titre="Stage d'automne", armes=["EPE"]), headers=ADMIN)
    assert r.status_code == 200 and r.json()["titre"] == "Stage d'automne" and r.json()["cree_le"] == m["cree_le"]
    assert [x["id"] for x in client.get("/manuelles", headers=ADMIN).json()] == [m["id"]]
    assert client.delete(f"/manuelles/{m['id']}", headers=ADMIN).status_code == 200
    assert client.get(f"/competitions/{m['id']}").status_code == 404
    assert client.delete(f"/manuelles/{m['id']}", headers=ADMIN).status_code == 404


def test_manuelle_validation_et_armes(client):
    for mauvais in (
        {"titre": ""}, {"categories": []}, {"categories": ["M99"]}, {"armes": ["XXX"]},
        {"date_fin": "2026-10-01"}, {"document_url": "javascript:alert(1)"}, {"delai_inscription_jours": 0},
    ):
        assert client.post("/manuelles", json=_manuelle(**mauvais), headers=ADMIN).status_code == 422, mauvais
    # Sans arme : affichée quelle que soit l'arme choisie ; plusieurs armes : une compétition par arme
    sans = client.post("/manuelles", json=_manuelle(), headers=ADMIN).json()
    client.post("/manuelles", json=_manuelle(titre="Tournoi", armes=["FLE", "SAB"]), headers=ADMIN)
    fle = client.get("/competitions", params={"source": "manuel", "arme": "FLE"}).json()["competitions"]
    assert {c["id"] for c in fle} >= {sans["id"]} and len(fle) == 2
    ids = {c["id"] for c in client.get("/competitions", params={"source": "manuel"}).json()["competitions"]}
    assert len(ids) == 3 and any(i.endswith(".SAB") for i in ids)
    assert client.get("/competitions", params={"source": "manuel", "categorie": "SENIOR"}).json()["count"] == 0


def test_manuelle_liee_ffe_fusionnee(client):
    # Une compétition de la liste FFE dont la fiche (page de test, la même pour tous les identifiants) concorde
    def concorde(c):
        if not (c["lieu"] and c["lieu"] != "Lieu indéterminé" and c["categories"]):
            return False
        f = client.get(f"/competitions/{c['id']}")
        return f.status_code == 200 and f.json()["lieu"] == c["lieu"] and f.json()["date_debut"] == c["date_debut"]

    ffe = next(c for c in client.get("/competitions", params={"source": "ffe"}).json()["competitions"] if concorde(c))
    saisie = _manuelle(titre="Notre tournoi", lieu=ffe["lieu"], date_debut=ffe["date_debut"], armes=ffe["armes"],
                       categories=ffe["categories"], remarque="Covoiturage", delai_inscription_jours=10, liee_ffe=True)
    m = client.post("/manuelles", json=saisie, headers=ADMIN).json()
    d = client.get("/competitions", params={"source": "ffe,manuel"}).json()["competitions"]
    fusion = next(c for c in d if c["id"] == ffe["id"])
    assert fusion["sources"] == ["ffe", "manuel"] and fusion["remarque"] == "Covoiturage"
    assert fusion["delai_inscription_jours"] == 10 and fusion["id_manuel"] == m["id"] and fusion["titre"] == ffe["titre"]
    assert not any(c["id"] == m["id"] for c in d)  # pas de doublon
    detail = client.get(f"/competitions/{ffe['id']}").json()
    assert detail.get("remarque") == "Covoiturage", (ffe, detail)
    # Non liée : affichée à part
    client.put(f"/manuelles/{m['id']}", json={**saisie, "liee_ffe": False}, headers=ADMIN)
    d = client.get("/competitions", params={"source": "ffe,manuel"}).json()["competitions"]
    assert any(c["id"] == m["id"] for c in d) and next(c for c in d if c["id"] == ffe["id"])["remarque"] is None


def test_manuelle_import_csv(client):
    modele = client.get("/manuelles/modele.csv")
    assert modele.status_code == 200 and modele.text.startswith("\ufeffnom;lieu;date_debut")
    r = client.post("/manuelles/import", files={"fichier": ("m.csv", modele.content, "text/csv")}, data={"essai": "true"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["essai"] and r.json()["nb"] == 2 and client.get("/manuelles", headers=ADMIN).json() == []

    r = client.post("/manuelles/import", files={"fichier": ("m.csv", modele.content, "text/csv")}, headers=ADMIN)
    assert r.json()["nb"] == 2
    liste = client.get("/manuelles", headers=ADMIN).json()
    noel = next(m for m in liste if m["titre"] == "Tournoi de Noël")
    assert noel["armes"] == ["EPE"] and noel["categories"] == ["M13", "M15", "M17"] and noel["delai_inscription_jours"] == 10
    assert noel["liee_ffe"] and noel["inscription_sur_place"] and noel["document_nom"] == "Invitation"
    stage = next(m for m in liste if m["titre"] == "Stage de Toussaint")
    assert stage["preinscription"] is False and stage["date_fin"] == "2026-10-28"

    # Windows-1252, séparateur virgule, en-têtes approximatifs ; une ligne fausse : rien n'est ajouté
    csv = "Nom,Lieu,Date,Catégories,Armes\nA,Massy,01/12/2026,M15,Épée\nB,Massy,32/12/2026,M15,Fleuret\nC,Massy,01/12/2026,M15,Hache\n"
    r = client.post("/manuelles/import", files={"fichier": ("m.csv", csv.encode("cp1252"), "text/csv")}, headers=ADMIN)
    assert r.status_code == 422 and "ligne 3" in r.json()["detail"] and "ligne 4" in r.json()["detail"]
    assert len(client.get("/manuelles", headers=ADMIN).json()) == 2
    r = client.post("/manuelles/import", files={"fichier": ("m.csv", b"nom;lieu\nA;B\n", "text/csv")}, headers=ADMIN)
    assert r.status_code == 422 and "date_debut" in r.json()["detail"]
