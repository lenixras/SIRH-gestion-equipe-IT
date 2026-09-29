"""Tests de bout en bout de l'import GLPI, sur le simulateur (mock-glpi/app.py) lance en vrai.

Couvre : lecture API (search + detail), chemin Session-Token d'une instance stricte, import
idempotent, filtre incremental, affectation d'un ticket et suivi (MTTA/MTTR) apres reimport.

    python3 -m pytest tests/test_glpi.py -q
"""
import copy
import importlib.util
import os
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest

# forcer la base de test (comme test_app.py) : ce fichier ecrit dans kpi_raw
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL",
                                            "postgresql://sirh:sirh@localhost:55432/sirh_test")
# le simulateur sert l'API sous /glpi/apirest.php (comme une instance derriere un sous-chemin) :
# on l'impose pour qu'un .env local (GLPI_API_PATH=/apirest.php) ne casse pas la CI
os.environ["GLPI_API_PATH"] = "/glpi/apirest.php"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, glpi  # noqa: E402

APP_TOKEN, USER_TOKEN = "simtoken", "simuser"
MOCK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mock-glpi", "app.py")


def _load(strict=False):
    spec = importlib.util.spec_from_file_location("glpi_sim", MOCK)
    mod = importlib.util.module_from_spec(spec)
    os.environ["MOCK_GLPI_STRICT"] = "1" if strict else "0"
    spec.loader.exec_module(mod)          # pylint: disable=protected-access
    return mod


def _serve(mod):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture(scope="module")
def sim():
    """Simulateur GLPI reel (conteneur equivalent) sur un port libre, etat fige pour chaque test."""
    mod = _load()
    srv = _serve(mod)
    pristine = copy.deepcopy(mod.TICKETS)
    yield mod, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    mod.TICKETS.clear()
    mod.TICKETS.update(pristine)


@pytest.fixture(autouse=True)
def clean_kpi():
    db.init()
    with db.connect() as c:   # v_tickets couvre toutes les sources : on repart d'une base propre
        c.execute("TRUNCATE kpi_raw")
    yield


def _sync(url, **kw):
    return glpi.sync(url=url, token=APP_TOKEN, user_token=USER_TOKEN, **kw)


def test_import_glpi_mesure_le_suivi(sim):
    """Le lot GLPI alimente kpi_raw et les vues de KPI (MTTA/MTTR par personne)."""
    mod, url = sim
    assert _sync(url) == len(mod.TICKETS)

    rows = db.query("SELECT * FROM v_tickets")
    assert len(rows) == len(mod.TICKETS)
    assert {r["status"] for r in rows} <= {"nouveau", "encours", "attente", "resolu"}
    assert all(r["title"] and r["opened_at"] for r in rows)

    # un ticket resolu a un MTTA et un MTTR, un ticket neuf n'a pas de resolution
    resolu = db.one("SELECT * FROM v_tickets WHERE status = 'resolu' AND mttr_s IS NOT NULL")
    assert resolu["mtta_s"] > 0 and resolu["mttr_s"] > 0
    assert db.one("SELECT count(*) n FROM v_tickets WHERE status = 'nouveau' AND solved_at IS NULL")["n"] == 2

    # les horodatages viennent bien du helpdesk (aucun chronometre recalcule par la plateforme)
    source = db.one("SELECT * FROM kpi_raw WHERE external_id = '1'")
    assert round((source["taken_at"] - source["opened_at"]).total_seconds()) == \
        mod.TICKETS[1]["takeintoaccount_delay_stat"]
    assert round((source["solved_at"] - source["taken_at"]).total_seconds()) == \
        mod.TICKETS[1]["solutiontime"]

    # KPI par personne : la charge suit l'affectation GLPI (rapprochement par nom)
    par = {r["name"]: r for r in db.query("SELECT * FROM v_kpi_by_person WHERE resolved > 0")}
    assert par["Priya"]["resolved"] == sum(1 for t in mod.TICKETS.values()
                                           if t["users_id_tech"] == "Priya" and t["status"] in (5, 6))


def test_import_est_idempotent(sim):
    """Relancer l'import (nuit + bouton KPI) ne duplique rien : upsert sur (source, external_id)."""
    mod, url = sim
    _sync(url)
    _sync(url)
    assert db.one("SELECT count(*) n FROM kpi_raw WHERE source='glpi'")["n"] == len(mod.TICKETS)


def test_session_token_obligatoire_en_mode_strict():
    """Une instance GLPI exigeant initSession : le client bascule sur Session-Token tout seul."""
    mod = _load(strict=True)
    srv = _serve(mod)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        import httpx
        with httpx.Client(timeout=5) as s:   # controle : le user_token seul ne passe pas
            r = s.get(url + "/glpi/apirest.php/search/Ticket",
                      headers={"App-Token": APP_TOKEN, "Authorization": "user_token " + USER_TOKEN})
            assert r.status_code == 401
        assert _sync(url) == len(mod.TICKETS)   # le client initSession puis rejoue
    finally:
        srv.shutdown()


def test_mauvais_app_token_refuse(sim):
    _, url = sim
    with pytest.raises(Exception):
        glpi.sync(url=url, token="mauvais", user_token=USER_TOKEN)


def test_filtre_incremental_depuis_une_date(sim):
    """GLPI_SINCE / --since : on ne rapporte que les tickets crees apres une date."""
    mod, url = sim
    assert _sync(url, since="2099-01-01") == 0
    assert db.one("SELECT count(*) n FROM kpi_raw WHERE source='glpi'")["n"] == 0
    assert _sync(url, since="2000-01-01") == len(mod.TICKETS)


def test_console_web_permet_de_simuler_un_ticket(sim):
    """La console HTML du simulateur : ouvrir, affecter, resoudre — le geste du helpdeck, au clic."""
    import httpx
    _, url = sim
    with httpx.Client(timeout=5, follow_redirects=False) as s:
        assert "Simuler un ticket" in s.get(url + "/").text
        r = s.post(url + "/ui/ticket/new", data={"name": "Poste de travail HS", "itilcategories_id": "1",
                                                 "priority": "4", "users_id_tech": ""})
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert s.post(url + "/ui/ticket/new", data={"name": "x"},
                      headers={"Origin": "http://evil.fr"}).status_code == 403   # CSRF

    cree = [t for t in glpi.fetch(url, APP_TOKEN, USER_TOKEN) if t["name"] == "Poste de travail HS"]
    assert len(cree) == 1 and cree[0]["status"] == 1
    tid = cree[0]["id"]
    with httpx.Client(timeout=5, follow_redirects=False) as s:      # affectation puis resolution
        for status in (2, 5):
            assert s.post(url + f"/ui/ticket/{tid}",
                          data={"status": str(status), "users_id_tech": "Priya"}).status_code == 303
    _sync(url)                                                      # l'import voit le nouveau ticket
    suivi = db.one("SELECT * FROM v_tickets WHERE external_id = %s", (str(tid),))
    assert (suivi["status"], suivi["person"]) == ("resolu", "Priya")


def test_affectation_et_suivi_d_un_ticket(sim):
    """Le helpdeck affecte un ticket a Priya et le passe en resolu : l'import suivant le voit."""
    mod, url = sim
    _sync(url)
    cible = mod.TICKETS[12]                     # "Wi-Fi instable" : nouveau, sans technicien
    assert cible["users_id_tech"] == ""

    import httpx
    with httpx.Client(timeout=5) as s:            # action du helpdeck, pas de la plateforme
        r = s.put(url + f"/glpi/apirest.php/Ticket/{cible['id']}",
                  headers={"App-Token": APP_TOKEN, "Authorization": "user_token " + USER_TOKEN,
                           "Content-Type": "application/json"},
                  json={"input": {"users_id_tech": "Priya", "status": 5}})
        assert r.status_code == 200

    _sync(url)                                   # synchronisation de la plateforme
    suivi = db.one("SELECT * FROM v_tickets WHERE external_id = %s", (str(cible["id"]),))
    assert suivi["person"] == "Priya"
    assert suivi["status"] == "resolu"
    assert suivi["mtta_s"] > 0 and suivi["mttr_s"] >= 0
    assert suivi["solved_at"] is not None


def test_champs_reels_glpi_11():
    """Formes observees sur une instance 11.0.9 : `data` en liste, solvedate, colonnes en dur."""
    assert glpi._rows({"data": [{"2": 1}]}) == [{"2": 1}]        # GLPI 11 renvoie une liste
    assert glpi._rows({"data": {"1": {"2": 1}}}) == [{"2": 1}]   # la doc GLPI renvoie un objet
    with db.connect() as c:
        row = glpi.normalize({"id": 99, "name": "t", "status": 5, "type": "1", "priority": "3",
                              "date_creation": "2026-09-01 10:00:00", "takeintoaccount_delay_stat": "60",
                              "solve_delay_stat": "3600", "solvedate": "2026-09-01 12:00:00"}, c)
    assert row[4] == 3                                          # priorite textuale -> int
    assert row[6].isoformat() == "2026-09-01T10:00:00+00:00"    # ouverture
    assert row[7].isoformat() == "2026-09-01T10:01:00+00:00"    # ouverture + delai de prise en charge
    assert row[8].isoformat() == "2026-09-01T12:00:00+00:00"    # solvedate l'emporte sur la duree
