"""Tests des protections : SSRF, liens, identifiants, jeton, en-têtes, tailles, injection iCalendar."""

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import cde91, ffe, idf
from app.config import Settings
from app.main import creer_app
from app.securite import AdresseInterdite, GardeReseau, LimiteTentatives, adresse_publique, lien_web, telecharger

from test_api import JETON_TEST, reseau

FIXTURES = Path(__file__).parent / "fixtures"


# --- Garde SSRF -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip, publique",
    [
        ("8.8.8.8", True), ("151.101.1.1", True),
        ("127.0.0.1", False), ("10.0.0.5", False), ("172.17.0.1", False), ("192.168.1.10", False),
        ("169.254.169.254", False), ("0.0.0.0", False), ("100.64.0.1", False), ("224.0.0.1", False),
        ("::1", False), ("fe80::1", False), ("fd00::1", False), ("::ffff:127.0.0.1", False),
    ],
)
def test_adresse_publique(ip, publique):
    assert adresse_publique(ip) is publique


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8765/calendriers", "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://localhost/",
     "http://10.1.2.3/x.pdf", "https://172.17.0.1:8080/login/"],
)
def test_garde_refuse_le_reseau_interne(url):
    garde = GardeReseau()
    with pytest.raises(AdresseInterdite):
        asyncio.run(garde(httpx.Request("GET", url)))


def test_garde_refuse_les_redirections_vers_le_reseau_interne():
    """Un site public qui redirige vers 127.0.0.1 est bloqué au moment de suivre la redirection."""
    async def scenario():
        def repondre(requete):
            if requete.url.host == "8.8.8.8":
                return httpx.Response(302, headers={"location": "http://127.0.0.1:8765/calendriers"})
            return httpx.Response(200, content=b"%PDF interne")
        async with httpx.AsyncClient(transport=httpx.MockTransport(repondre), follow_redirects=True,
                                     event_hooks={"request": [GardeReseau()]}) as http:
            await http.get("http://8.8.8.8/calendrier.pdf")
    with pytest.raises(AdresseInterdite):
        asyncio.run(scenario())


def test_telechargement_borne():
    async def scenario(limite):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 5000))) as http:
            return await telecharger(http, "https://exemple.fr/gros.pdf", limite)
    assert len(asyncio.run(scenario(10000)).content) == 5000
    with pytest.raises(ValueError, match="trop volumineux"):
        asyncio.run(scenario(1000))


# --- Liens et identifiants ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, garde",
    [("https://cde91.fr/a.pdf", True), ("http://x.fr/b.pdf", True), ("javascript:alert(1)//a.pdf", False),
     ("data:text/html,<script>", False), ("file:///etc/passwd", False), ("//x.fr/a.pdf", False), ("", False), (None, False)],
)
def test_lien_web(url, garde):
    assert (lien_web(url) is not None) is garde


def test_liens_des_pages_http_seulement():
    """Un site piraté qui glisse « javascript:…//x.pdf » : le lien est écarté (il finirait dans un href)."""
    page = '<div itemprop="articleBody"><a href="javascript:alert(document.cookie)//cal.pdf">Calendrier</a>' \
           '<a href="/images/cal.pdf">Calendrier</a></div>'
    assert [d.url for d in cde91.liens_pdf(page, "https://cde91.fr/page")] == ["https://cde91.fr/images/cal.pdf"]
    page_idf = '<a href="javascript:alert(1)//x.pdf">Calendrier IDF Fleuret</a><a href="https://l.fr/f.pdf">Calendrier IDF Epée</a>'
    assert [d.url for d in idf.liens_calendriers(page_idf, "https://l.fr/")] == ["https://l.fr/f.pdf"]


def test_note_ffe_http_seulement():
    html = (FIXTURES / "ffe_detail_avec_note.html").read_text(encoding="utf-8")
    html = html.replace("https://dirigeant.escrime-ffe.fr/engagement/telecharger/1/107310", "javascript:alert(1)")
    assert ffe.analyser_fiche(html).note_organisation is None


@pytest.mark.parametrize("id_", ["ffe-../../wp-admin", "ffe-a b", "ffe-%2e%2e", "ffe-", "ffe-" + "A" * 600])
def test_identifiant_ffe_refuse(id_):
    with pytest.raises(ValueError):
        ffe.jeton_depuis_id(id_)


# --- API ---------------------------------------------------------------------------------------


def _client(tmp_path, **options):
    settings = Settings(data_dir=tmp_path, public_url="https://api.test", _env_file=None, **options)
    return TestClient(creer_app(settings, demarrer_taches=False, transport=reseau([])))


def test_jeton_trop_court_desactive_l_administration(tmp_path):
    with _client(tmp_path, admin_token="court") as c:
        r = c.post("/calendriers/idf/refresh", headers={"Authorization": "Bearer court"})
        assert r.status_code == 503 and "trop court" in r.json()["detail"]
    with _client(tmp_path, admin_token="") as c:
        assert c.post("/calendriers/idf/refresh").status_code == 503


def test_tentatives_de_jeton_limitees(tmp_path):
    with _client(tmp_path, admin_token=JETON_TEST) as c:
        for _ in range(10):
            assert c.post("/calendriers/idf/refresh", headers={"Authorization": "Bearer mauvais"}).status_code == 401
        # 11e tentative : bloquée, même avec le bon jeton
        assert c.post("/calendriers/idf/refresh", headers={"Authorization": "Bearer " + JETON_TEST}).status_code == 429


def test_limite_tentatives_fenetre():
    limite = LimiteTentatives(maximum=2, fenetre=0.05)
    limite.echec("1.2.3.4")
    limite.echec("1.2.3.4")
    assert limite.bloquee("1.2.3.4") and not limite.bloquee("5.6.7.8")
    import time
    time.sleep(0.06)
    assert not limite.bloquee("1.2.3.4")


def test_entetes_de_securite(tmp_path):
    with _client(tmp_path) as c:
        r = c.get("/health")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert "default-src 'none'" in r.headers["content-security-policy"]
        assert "content-security-policy" not in c.get("/docs").headers  # Swagger a besoin de ses scripts


def test_documentation_desactivable(tmp_path):
    with _client(tmp_path, docs=False) as c:
        assert c.get("/docs").status_code == 404
        assert c.get("/openapi.json").status_code == 404
        assert c.get("/redoc").status_code == 404


def test_identifiant_invalide_404(tmp_path):
    with _client(tmp_path) as c:
        assert c.get("/competitions/ffe-..%2F..%2Fwp-admin").status_code == 404
        assert c.get("/competitions/ffe-a%20b/note", follow_redirects=False).status_code == 404


def test_fichier_depose_trop_gros(tmp_path, monkeypatch):
    import app.main
    monkeypatch.setattr(app.main, "TAILLE_MAX_PDF", 1000)
    with _client(tmp_path, admin_token=JETON_TEST) as c:
        r = c.post("/calendriers/idf/cibles/fleuret", headers={"Authorization": "Bearer " + JETON_TEST},
                   files={"fichier": ("gros.pdf", b"%PDF" + b"x" * 5000, "application/pdf")})
        assert r.status_code == 413


def test_injection_ics(tmp_path):
    with _client(tmp_path) as c:
        r = c.get("/competitions.ics", params={"source": "cde91", "nom": "Club\r\nBEGIN:VEVENT\r\nSUMMARY:Pirate"})
        assert r.status_code == 200
        lignes = r.text.split("\r\n")
        assert not any(l.startswith("SUMMARY:Pirate") for l in lignes)
        assert not any(l.startswith("BEGIN:VEVENT") for l in lignes)  # le texte injecté reste dans la ligne du nom, échappé
        assert any(l.startswith("X-WR-CALNAME:Club") for l in lignes)


def test_textes_trop_longs_refuses(tmp_path):
    with _client(tmp_path) as c:
        assert c.get("/competitions", params={"ville": "x" * 200}).status_code == 422
        assert c.get("/competitions.ics", params={"nom": "x" * 500}).status_code == 422
        assert c.get("/competitions", params={"niveau": "1;DROP"}).status_code == 422
