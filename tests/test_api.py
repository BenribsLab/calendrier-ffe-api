"""Tests des routes, avec un faux réseau (httpx.MockTransport) servant les pages enregistrées."""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import creer_app
from app.service import departements_du_titre

FIXTURES = Path(__file__).parent / "fixtures"

COMMUNES = {
    "SAVIGNY-SUR-ORGE": ("Savigny-sur-Orge", "91", "11"),
    "SAVIGNY S/ ORGE": ("Savigny-sur-Orge", "91", "11"),
    "SAVIGNY": ("Savigny-sur-Orge", "91", "11"),
    "MENNECY": ("Mennecy", "91", "11"),
    "CHILLY MAZARIN": ("Chilly-Mazarin", "91", "11"),
    "ETAMPES": ("Étampes", "91", "11"),
    "MASSY": ("Massy", "91", "11"),
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
        if url.host == "escrime-iledefrance.fr" and url.path.endswith(".pdf"):
            return httpx.Response(200, content=(FIXTURES / "idf_calendrier.pdf").read_bytes())
        if url.host == "escrime-iledefrance.fr":
            return httpx.Response(200, text=(FIXTURES / "idf_page.html").read_text(encoding="utf-8"))
        if url.host == "geo.api.gouv.fr" and url.path == "/regions":
            return httpx.Response(200, json=[{"nom": "Île-de-France", "code": "11"}])
        if url.host == "geo.api.gouv.fr" and url.path == "/departements":
            return httpx.Response(200, json=[{"nom": "Essonne", "code": "91", "codeRegion": "11"}])
        if url.host == "geo.api.gouv.fr":
            c = COMMUNES.get(url.params["nom"])
            return httpx.Response(200, json=[{"nom": c[0], "codeDepartement": c[1], "codeRegion": c[2]}] if c else [])
        return httpx.Response(404)

    return httpx.MockTransport(repondre)


@pytest.fixture
def client(tmp_path):
    appels: list[str] = []
    settings = Settings(data_dir=tmp_path, admin_token="secret", public_url="https://api.test", _env_file=None)
    app = creer_app(settings, demarrer_taches=False, transport=reseau(appels))
    with TestClient(app) as c:
        for source in ("cde91", "idf"):
            c.post(f"/calendriers/{source}/refresh", headers={"Authorization": "Bearer secret"})
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
