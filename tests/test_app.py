"""Test de bout en bout : auth, RBAC, ecrans, vues KPI, ecriture unique (dashboard/API/MCP).

    DATABASE_URL=postgresql://sirh:sirh@localhost:55432/sirh_test python3 -m pytest tests -q
"""
import asyncio
import os
import sys

import pytest

# forcer la base de test : ce fichier fait des TRUNCATE, jamais la base de travail
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL",
                                            "postgresql://sirh:sirh@localhost:55432/sirh_test")
os.environ.pop("MCP_WRITE", None)          # ecriture MCP desactivee par defaut
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import db, glpi, services  # noqa: E402
from app.auth import Denied, hash_password  # noqa: E402
from app.mcp_server import mcp  # noqa: E402
from app.main import app  # noqa: E402

PW = "secret123"
BASIC_MGR = ("manager@example.com", PW)
BASIC_TECH = ("priya@example.com", PW)


@pytest.fixture(scope="module", autouse=True)
def seed():
    db.init()
    with db.connect() as c:
        c.execute("TRUNCATE persons, projects, skills, skills_log, assignments, notes, kpi_raw, objectives,"
                  " audit_log, roles, role_permissions, sessions, support, categories"
                  " RESTART IDENTITY CASCADE")
        db.init()   # le TRUNCATE a vide roles/permissions : on relit schema.sql pour retrouver le seed
        c.execute("INSERT INTO persons (name, email, role, title, password_hash) VALUES"
                  " ('Marc','manager@example.com','manager','lead',%s),"
                  " ('Priya','priya@example.com','tech','sysadmin',%s)", [hash_password(PW)] * 2)
        c.execute("INSERT INTO projects (name, status, due_date) VALUES ('Migration NAS','running', current_date - 3)")
        c.execute("INSERT INTO assignments (project_id, person_id, role, status, due_date) VALUES"
                  " (1, 2, 'techin', 'in_progress', current_date - 1)")
        c.execute("INSERT INTO notes (assignment_id, author_id, text, progress)"
                  " VALUES (1, 2, 'sauvegarde en cours', 30)")
        c.execute("INSERT INTO kpi_raw (source, external_id, person_id, category, opened_at, taken_at, solved_at)"
                  " VALUES ('test','1',2,'incident', now() - interval '3 h', now() - interval '2 h',"
                  " now() - interval '30 min')")
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------------ auth / RBAC

def test_screens_require_auth(client):
    """Le HTML renvoie vers /login ; l'API garde son 401 + defi Basic (les scripts n'ont pas de cookie)."""
    for path in ("/equipe", "/competences", "/projets", "/kpi", "/personnes/2"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
    assert client.get("/api/kpis").status_code == 401


def test_401_carries_the_basic_challenge(client):
    """Sans cet en-tete le navigateur n'affiche aucune boite de dialogue : c'est le 401 muet."""
    r = client.get("/api/kpis")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == 'Basic realm="Mini-SIRH equipe"'
    assert "identifiants requis" in r.text


def test_four_screens_render(client):
    for path, marker in (("/equipe", "Marc"), ("/competences", "chartPersonnes"), ("/projets", "Jalons"),
                         ("/kpi", "MTTR"), ("/personnes/2", "Migration NAS"), ("/r/affectations/new", "Projet")):
        r = client.get(path, auth=BASIC_MGR)
        assert r.status_code == 200 and marker in r.text


def test_bad_password_rejected(client):
    assert client.get("/api/kpis", auth=("manager@example.com", "nope")).status_code == 401
    assert client.post("/login", data={"email": "manager@example.com", "password": "nope"},
                       follow_redirects=False).headers["location"].startswith("/login?err=")


def test_tech_cannot_create_person_but_can_log_progress(client):
    assert client.post("/api/personnes", auth=BASIC_TECH, json={"name": "X", "email": "x@y.z",
                                                               "password": "pw"}).status_code == 403
    r = client.post("/api/personnes", auth=BASIC_MGR,
                    json={"name": "Doublon", "email": "manager@example.com", "password": "pw"})
    assert r.status_code == 409, r.text  # unicite
    # note sur sa propre affectation : OK (303 = redirection post-ecriture)
    assert client.post("/notes", auth=BASIC_TECH, follow_redirects=False,
                       data={"assignment_id": 1, "text": "NAS migre a 80%",
                             "progress": 80}).status_code == 303
    note = db.one("SELECT * FROM notes ORDER BY at DESC LIMIT 1")
    assert note["text"].startswith("NAS") and note["author_id"] == 2
    assert db.one("SELECT progress FROM assignments WHERE id=1")["progress"] == 80


def test_tech_cannot_touch_other_assignment():
    tech = {"id": 2, "role": "tech", "email": "priya@example.com"}
    other = services.create_assignment({"id": 1, "role": "manager", "email": "m@x.fr"}, "api",
                                       {"project_id": 1, "person_id": 1, "role": "lead"})
    with pytest.raises(Exception):
        services.add_note(tech, "api", {"assignment_id": other["id"], "text": "pas pour moi"})


# ------------------------------------------------------------------ ecriture unique

def test_skill_upsert_logs_dated_evolution():
    services.upsert_skill({"id": 1, "role": "manager", "email": "m@x.fr"}, "api",
                          {"person_id": 2, "category": "serveur", "level": 3, "note": "autonome"})
    services.upsert_skill({"id": 1, "role": "manager", "email": "m@x.fr"}, "api",
                          {"person_id": 2, "category": "serveur", "level": 4, "note": "migration NAS"})
    hist = [r for r in db.query("SELECT * FROM skills_log WHERE skill='serveur' ORDER BY at, id")]
    assert [h["level"] for h in hist] == [3, 4] and hist[1]["previous_level"] == 3
    matrix = db.one("SELECT * FROM v_skill_matrix WHERE person_id=2")
    assert matrix["levels"] == {"Serveur / stockage": 4}
    with db.connect() as c:                                        # ne pas fausser les tests suivants
        c.execute("DELETE FROM skills WHERE person_id=2")
        c.execute("DELETE FROM skills_log WHERE person_id=2")


def test_skill_levels_are_per_person_per_category(client):
    """Une competence est definie (personne, categorie) : un niveau par categorie, refus sinon."""
    assert client.post("/api/competences", auth=BASIC_MGR,
                       json={"person_id": 2, "category": "reseau", "level": 5,
                             "note": "accords VLAN"}).status_code == 201
    assert client.post("/api/competences", auth=BASIC_MGR,
                       json={"person_id": 2, "category": "helpdesk", "level": 5}).status_code == 201
    assert client.post("/api/competences", auth=BASIC_MGR,
                       json={"person_id": 2, "category": "nage-poney", "level": 5}).status_code == 400
    assert client.post("/api/competences", auth=BASIC_MGR,
                       json={"person_id": 2, "category": "reseau", "level": 9}).status_code == 400
    levels = db.one("SELECT * FROM v_skill_matrix WHERE person_id=2")["levels"]
    assert levels == {"Reseau": 5, "Helpdesk / poste de travail": 5}
    with db.connect() as c:
        c.execute("DELETE FROM skills WHERE person_id=2")   # ne pas fausser les tests suivants
        c.execute("DELETE FROM skills_log WHERE person_id=2")


def test_every_write_is_audited(client):
    assert client.post("/api/personnes", auth=BASIC_MGR,
                       json={"name": "Nina", "email": "nina@example.com", "password": "pw",
                             "role": "tech"}).status_code == 201
    rows = db.query("SELECT source, actor, action, entity FROM audit_log ORDER BY id")
    assert any(r["source"] == "api" and r["entity"] == "person" and r["actor"] == "manager@example.com"
               for r in rows)


# ------------------------------------------------------------------ kanban

def test_kanban_screen_and_api(client):
    r = client.get("/kanban", auth=BASIC_MGR)
    assert r.status_code == 200 and "Migration NAS" in r.text
    assert all(label in r.text for label in ("A faire", "En cours", "Bloque", "Termine"))
    api = client.get("/api/kanban", auth=BASIC_MGR).json()
    assert [c["key"] for c in api["cols"]] == ["todo", "in_progress", "blocked", "done"]
    assert any(c["person"] == "Priya" for c in api["cards"])
    assert client.get("/kanban", follow_redirects=False).status_code == 303


def test_kanban_move_changes_status_and_is_audited(client):
    """Le drop du navigateur et le select du formulaire passent par la meme ecriture unique."""
    avant = db.one("SELECT progress FROM assignments WHERE id=1")["progress"]
    r = client.post("/kanban/1", auth=BASIC_TECH, follow_redirects=False,
                    data={"status": "blocked"},
                    headers={"referer": "http://testserver/kanban"})
    assert r.status_code == 303 and r.headers["location"] == "http://testserver/kanban"
    row = db.one("SELECT status, progress FROM assignments WHERE id=1")
    assert row["status"] == "blocked" and row["progress"] == avant   # un deplacement ne touche pas la jauge
    log = db.one("SELECT * FROM audit_log WHERE entity='assignment' AND entity_id='1' ORDER BY id DESC LIMIT 1")
    assert log["source"] == "kanban" and log["actor"] == "priya@example.com"
    assert log["detail"]["status"] == "blocked"


def test_kanban_rejects_bad_status_and_foreign_card(client):
    """Pire cas du client (formulaire bricole) : on revient a l'ecran avec un message, 303."""
    r = client.post("/kanban/1", auth=BASIC_TECH, follow_redirects=False,
                    data={"status": "bouge"}, headers={"referer": "http://testserver/kanban"})
    assert r.status_code == 303 and "err=" in r.headers["location"]
    assert db.one("SELECT status FROM assignments WHERE id=1")["status"] == "blocked"   # inchange
    r = client.post("/kanban/1", auth=BASIC_TECH, follow_redirects=False,
                    data={"status": "done"}, headers={"referer": "http://testserver/kanban", "origin": "http://evil.fr"})
    assert r.status_code == 403                                            # CSRF : origine refusee
    r = client.post("/kanban/999", auth=BASIC_MGR, follow_redirects=False,   # carte fantome
                    data={"status": "done"}, headers={"referer": "http://testserver/kanban"})
    assert r.status_code == 303 and "introuvable" in r.headers["location"]


def test_kanban_access_follows_assignment_rights(client):
    """Un technicien ne bouge pas la carte d'un autre (meme regle que l'edition d'affectation)."""
    other = services.create_assignment({"id": 1, "role": "manager", "email": "m@x.fr"}, "api",
                                       {"project_id": 1, "person_id": 1, "role": "lead"})
    with pytest.raises(Denied):
        services.set_assignment_status({"id": 2, "role": "tech", "email": "priya@example.com"},
                                       "kanban", other["id"], "done")
    # l'API de la meme couche accepte en plus la jauge (utilisee par le suivi de notes)
    mgr = {"id": 1, "role": "manager", "email": "manager@example.com"}
    services.set_assignment_status(mgr, "api", 1, "in_progress", progress=45)
    assert db.one("SELECT status, progress FROM assignments WHERE id=1")["status"] == "in_progress"
    assert db.one("SELECT progress FROM assignments WHERE id=1")["progress"] == 45


# ------------------------------------------------------------------ ecrans KPI

def test_json_api_exposes_aggregates(client):
    assert client.get("/api/kpis", auth=BASIC_MGR).json()["summary"]["resolved"] >= 0
    assert client.get("/api/who-is-on", auth=BASIC_MGR).json()["items"]
    assert client.get("/api/persons/2/history", auth=BASIC_MGR).json()["name"] == "Priya"
    assert client.get("/api/audit", auth=BASIC_MGR).status_code == 200
    assert client.get("/api/audit", auth=BASIC_TECH).status_code == 403   # journal : manager


def test_kpi_views_compute_mtta_mttr():
    summary = db.one("SELECT * FROM v_kpi_summary")
    assert summary["total"] == 1 and summary["resolved"] == 1
    assert 3500 < float(summary["mtta_s"]) < 3700    # ~1h
    assert 5300 < float(summary["mttr_s"]) < 5500    # 2h - 30min de prise en charge
    # cycle = ouverture -> resolution = MTTA + MTTR (MTTD/MTTR du pilotage)
    assert 8900 < float(summary["cycle_s"]) < 9100
    person = db.one("SELECT * FROM v_kpi_by_person WHERE person_id=2")
    assert person["resolved"] == 1
    assert float(person["cycle_s"]) == pytest.approx(float(person["mtta_s"]) + float(person["mttr_s"]), abs=1)
    assert db.query("SELECT * FROM v_late_milestones")[0]["name"] == "Migration NAS"
    assert db.one("SELECT overdue FROM v_open_assignments WHERE id=1")["overdue"] is True


def test_objectives_pct():
    services.set_objective({"id": 1, "role": "manager", "email": "m@x.fr"}, "api",
                           {"person_id": 2, "period": "2026-Q1", "title": "reduire le MTTR", "target": 10,
                            "achieved": 7})
    assert db.one("SELECT * FROM v_objectives_pct WHERE person_id=2")["pct"] == 70


# ------------------------------------------------------------------ MCP

def call(tool, **kw):
    async def go():
        from fastmcp import Client

        async with Client(mcp) as c:
            return await c.call_tool(tool, kw)
    return asyncio.run(go())


def test_mcp_reads_work():
    tools = asyncio.run(_list_tools())
    assert {"who_is_on", "assign_task", "log_progress", "get_kpis", "get_person_history"} <= tools
    assert "Priya" in call("who_is_on").content[0].text
    assert "mttr_s" in call("get_kpis").content[0].text
    assert "skills" in call("get_person_history", person="Priya").content[0].text


def test_mcp_write_gate_blocks_then_allows():
    with pytest.raises(Exception) as err:
        call("assign_task", project="Migration NAS", person="Priya", role="backup")
    assert "MCP_WRITE" in str(err.value)
    os.environ["MCP_WRITE"] = "1"
    os.environ["MCP_ACTOR_EMAIL"] = "manager@example.com"
    try:
        out = call("assign_task", project="Migration NAS", person="Priya", role="backup").content[0].text
        assert db.one("SELECT * FROM assignments WHERE role='backup'") is not None
        assert "backup" in out
        src = db.one("SELECT source FROM audit_log WHERE entity='assignment' ORDER BY id DESC LIMIT 1")["source"]
        assert src == "mcp"
    finally:
        os.environ.pop("MCP_WRITE", None)


def test_mcp_is_mounted_on_the_same_backend(client):
    r = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                              "clientInfo": {"name": "t", "version": "1"}}})
    assert r.status_code != 404


async def _list_tools():
    from fastmcp import Client

    async with Client(mcp) as c:
        return {t.name for t in await c.list_tools()}


# ------------------------------------------------------------------ identifiant GLPI

def test_glpi_id_is_stored_shown_and_unique(client):
    """L'identifiant du technicien GLPI se renseigne a la creation, s'affiche, ne se double pas."""
    r = client.post("/api/personnes", auth=BASIC_MGR,
                    json={"name": "Alex", "email": "alex@x.fr", "password": "pw", "glpi_id": 77})
    assert r.status_code == 201, r.text
    assert r.json()["glpi_id"] == 77
    assert "ID GLPI" in client.get("/equipe", auth=BASIC_MGR).text
    # completable plus tard sur la fiche existante
    assert client.put(f"/api/personnes/{r.json()['id']}", auth=BASIC_MGR,
                      json={"name": "Alex", "email": "alex@x.fr", "glpi_id": 78}).json()["glpi_id"] == 78
    assert client.get(f"/api/persons/{r.json()['id']}/history", auth=BASIC_MGR).json()["glpi_id"] == 78
    # unicite + validation du format
    assert client.post("/api/personnes", auth=BASIC_MGR,
                       json={"name": "Ali", "email": "ali@x.fr", "password": "pw", "glpi_id": 78}
                       ).status_code == 409
    assert client.post("/api/personnes", auth=BASIC_MGR,
                       json={"name": "Al", "email": "al@x.fr", "password": "pw", "glpi_id": "abc"}
                       ).status_code == 400


def test_glpi_id_replaces_name_lookup_for_homonyms():
    """Deux personnes du meme nom : l'identifiant GLPI tranche, le nom seul ne peut pas."""
    mgr = {"id": 1, "role": "manager", "email": "m@x.fr"}
    a = services.create_person(mgr, "api", {"name": "Cyril", "email": "c1@x.fr", "password": "pw", "glpi_id": 41})
    b = services.create_person(mgr, "api", {"name": "Cyril", "email": "c2@x.fr", "password": "pw", "glpi_id": 42})
    glpi.import_tickets([{"id": "9001", "name": "t", "status": 1, "type": 1, "priority": 3,
                          "date_creation": "2026-01-05 10:00:00", "users_id_tech": 42, "users_name": "Cyril"}])
    row = db.one("SELECT person_id FROM kpi_raw WHERE external_id = '9001'")
    assert row["person_id"] == b["id"] != a["id"]
    for pid in (a["id"], b["id"]):          # la suite compte sur 2 personnes et 1 ticket
        with db.connect() as c:
            c.execute("DELETE FROM kpi_raw WHERE person_id = %s", (pid,))
            c.execute("DELETE FROM persons WHERE id = %s", (pid,))


# ------------------------------------------------------------------ roles et permissions

def test_roles_matrix_drives_rights(client):
    """Un role sans permission ne cree pas de personne ; le role qui la porte, si."""
    r = client.post("/roles/new", auth=BASIC_MGR, data={"name": "recrue", "description": "recrue interne"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "recrue" in client.get("/roles", auth=BASIC_MGR).text
    # un technicien ne gere pas les roles
    assert client.post("/roles/new", auth=BASIC_TECH, data={"name": "pirate"},
                       follow_redirects=False).headers["location"].startswith("/roles?err=")

    def set_role(role):
        return client.put("/api/personnes/2", auth=BASIC_MGR,
                          json={"name": "Priya", "email": "priya@example.com", "role": role})

    set_role("recrue")
    assert client.post("/api/personnes", auth=BASIC_TECH,
                       json={"name": "X", "email": "x2@y.z", "password": "pw"}).status_code == 403
    assert client.get("/api/audit", auth=BASIC_TECH).status_code == 403

    # la meme personne, des que le role porte les permissions
    client.post("/roles/recrue/perms", auth=BASIC_MGR,          # champ repete : liste sous une meme cle
                data={"perm": ["persons.manage", "audit.read"]}, follow_redirects=False)
    assert client.get("/api/audit", auth=BASIC_TECH).status_code == 200
    created = client.post("/api/personnes", auth=BASIC_TECH,
                          json={"name": "X", "email": "x2@y.z", "password": "pw"})
    assert created.status_code == 201, created.text

    # un role encore porte ne se supprime pas ; la matrice reste en base apres l'echec
    assert client.post("/roles/recrue/delete", auth=BASIC_MGR, follow_redirects=False
                       ).headers["location"].startswith("/roles?err=")
    set_role("tech")
    assert client.post("/roles/recrue/delete", auth=BASIC_MGR, follow_redirects=False
                       ).headers["location"].startswith("/roles?msg=")
    with pytest.raises(Denied):
        services.require({"id": 2, "role": "tech", "email": "priya@example.com"}, "persons.manage")
    # une permission fantome est refusee avant ecriture
    with pytest.raises(ValueError):
        services.set_role_permissions({"id": 1, "role": "manager", "email": "m@x.fr"}, "api", "tech",
                                      ["tout.pouvoir"])
    assert client.get("/api/personnes", auth=BASIC_MGR).json()["items"]


def test_role_change_needs_roles_manage(client):
    """Un role qui cree des personnes ne peut pas s'auto-attribuer le role de gestion."""
    client.post("/roles/new", auth=BASIC_MGR, data={"name": "promu"}, follow_redirects=False)
    client.post("/roles/promu/perms", auth=BASIC_MGR, data={"perm": "persons.manage"}, follow_redirects=False)
    client.put("/api/personnes/2", auth=BASIC_MGR, json={"name": "Priya", "email": "priya@example.com", "role": "promu"})
    assert client.put("/api/personnes/2", auth=BASIC_TECH,
                      json={"name": "Priya", "email": "priya@example.com", "role": "manager"}).status_code == 403
    assert db.one("SELECT role FROM persons WHERE id=2")["role"] == "promu"
    client.put("/api/personnes/2", auth=BASIC_MGR, json={"name": "Priya", "email": "priya@example.com", "role": "tech"})
    client.post("/roles/promu/delete", auth=BASIC_MGR, follow_redirects=False)


# ------------------------------------------------------------------ session navigateur

def test_login_then_logout_kills_the_session(client):
    """Le logout n'existe pas en HTTP Basic : c'est le cookie de session qui permet de sortir."""
    assert client.get("/equipe", auth=BASIC_MGR).cookies == {}      # Basic : rien a deconnecter
    client.post("/login", data={"email": "manager@example.com", "password": PW}, follow_redirects=False)
    assert "sirh_session" in client.cookies
    assert client.get("/equipe").status_code == 200                 # la session suffit
    client.get("/logout", follow_redirects=False)
    assert "sirh_session" not in client.cookies
    assert client.get("/equipe", follow_redirects=False).headers["location"].startswith("/login")
    assert db.one("SELECT count(*) n FROM sessions")["n"] == 0


def test_login_next_cannot_leave_the_site(client):
    r = client.post("/login", data={"email": "manager@example.com", "password": PW,
                                    "next": "https://exemple.test/steal"},
                    follow_redirects=False)
    assert r.headers["location"] == "/equipe"
    client.get("/logout", follow_redirects=False)   # ne pas laisser la session ouverte pour la suite


# ------------------------------------------------------------------ support et analyse par categorie

SUPPORT_JSON = {"person_id": 2, "category": "reseau", "title": "Panne Internet globale",
                "reported_at": "2026-09-20T09:00", "resolved_at": "2026-09-20T11:30",
                "solution": "Boutique repliee sur la 4G"}


def test_support_is_recorded_via_api_and_api_analysis(client):
    """L'intervention s'enregistre par l'API, se relit (MTTR inclu) et alimente l'analyse."""
    r = client.post("/api/support", auth=BASIC_MGR, json=SUPPORT_JSON)
    assert r.status_code == 201, r.text
    assert r.json()["category"] == "reseau"
    row = db.one("SELECT * FROM v_support WHERE id = %s", (r.json()["id"],))
    assert row["person"] == "Priya" and int(row["mttr_s"]) == 9000      # 2h30
    assert client.get("/api/support", auth=BASIC_MGR).json()["items"]
    assert client.get("/api/competences/analyse", auth=BASIC_MGR).status_code == 200
    with db.connect() as c: c.execute("DELETE FROM support")                     # isoler l'analyse par categorie


def test_support_screen_and_person_page_render(client):
    services.create_support({"id": 1, "role": "manager", "email": "m@x.fr"}, "test", dict(SUPPORT_JSON))
    assert "Panne Internet globale" in client.get("/support", auth=BASIC_MGR).text
    assert "Interventions support" in client.get("/personnes/2", auth=BASIC_MGR).text
    with db.connect() as c: c.execute("DELETE FROM support")


def test_support_guard_dates_categories_and_update(client):
    d = dict(SUPPORT_JSON)
    d["resolved_at"] = "2026-09-20T08:00"                # resolution avant le signalement
    assert client.post("/api/support", auth=BASIC_MGR, json=d).status_code == 400
    d["resolved_at"] = SUPPORT_JSON["resolved_at"]
    d["category"] = "charlatan"
    assert client.post("/api/support", auth=BASIC_MGR, json=d).status_code == 400
    created = client.post("/api/support", auth=BASIC_MGR, json=SUPPORT_JSON).json()
    up = client.put(f"/api/support/{created['id']}", auth=BASIC_MGR,
                    json={**SUPPORT_JSON, "title": "Panne entreprise renommee"})
    assert up.status_code == 200 and up.json()["title"].startswith("Panne entreprise")
    with db.connect() as c: c.execute("DELETE FROM support")


def test_support_mttd_measures_reaction_not_backfill(client):
    """MTTD = delai entre l'ouverture de la fiche et le signalement terrain ; une fiche saisie
    apres coup (signalement anterieur a la creation) ne le connait pas : NULL, et donc exclue."""
    with db.connect() as c:
        c.execute("INSERT INTO support (person_id, category, title, reported_at, created_at)"
                  " VALUES (2,'reseau','En temps reel', now() - interval '10 min', now() - interval '30 min')")
        c.execute("INSERT INTO support (person_id, category, title, reported_at)"
                  " VALUES (2,'serveur','Saisie apres coup', now() - interval '2 h')")
    real = db.one("SELECT mttd_s FROM v_support WHERE title='En temps reel'")
    back = db.one("SELECT mttd_s FROM v_support WHERE title='Saisie apres coup'")
    assert abs(real["mttd_s"] - 1200) < 300                # ~20 min de reaction
    assert back["mttd_s"] is None
    with db.connect() as c:                                 # ne pas fausser les tests suivants
        c.execute("DELETE FROM support")


def test_tech_logs_own_support_but_not_someone_elses(client):
    """Meme regle de ligne que les competences : sa fiche a soi va, celle de l'autre exige que le
    role porte support.manage (le manager, lui, l'a)."""
    own = dict(SUPPORT_JSON); own["person_id"] = 2
    assert client.post("/api/support", auth=BASIC_TECH, json=own).status_code == 201
    other = dict(SUPPORT_JSON); other["person_id"] = 1
    assert client.post("/api/support", auth=BASIC_TECH, json=other).status_code == 403
    assert "sa propre fiche" in client.post("/api/support", auth=BASIC_TECH, json=other).text
    assert client.get("/support", auth=BASIC_TECH).status_code == 200
    with db.connect() as c: c.execute("DELETE FROM support")


def test_karim_touch_ni_interventions_ni_equipe(client):
    """Karim (tech, sans permission) ne modifie ni ne supprime d'intervention — meme la sienne —
    ni la fiche d'un collegue ; il ne supprime personne. Le manager lui peut.
    Rappel : modifier sa propre fiche reste autorise (regle de ligne)."""
    with db.connect() as c:
        c.execute("INSERT INTO persons (name, email, role, title, password_hash) VALUES"
                  " ('Karim','karim@example.com','tech','sysadmin',%s)", [hash_password(PW)])
        karim_id = c.execute("SELECT id FROM persons WHERE email='karim@example.com'").fetchone()["id"]
    karim = ("karim@example.com", PW)

    own = dict(SUPPORT_JSON); own["person_id"] = karim_id
    assert client.post("/api/support", auth=karim, json=own).status_code == 201
    support_id = db.one("SELECT id FROM support WHERE person_id=%s", (karim_id,))["id"]
    assert client.put(f"/api/support/{support_id}", auth=karim,
                      json={**own, "title": "titre pirate"}).status_code == 403
    assert client.delete(f"/api/support/{support_id}", auth=karim).status_code == 403

    assert client.put("/api/personnes/1", auth=karim,
                      json={"name": "Karl", "email": "manager@example.com"}).status_code == 403
    assert client.delete(f"/api/personnes/{karim_id}", auth=karim).status_code == 403

    assert client.put(f"/api/support/{support_id}", auth=BASIC_MGR,
                      json={**own, "title": "Intervention renommee"}).status_code == 200
    assert client.delete(f"/api/support/{support_id}", auth=BASIC_MGR).status_code == 200

    with db.connect() as c:
        c.execute("DELETE FROM support")
        c.execute("DELETE FROM skills WHERE person_id=%s", (karim_id,))
        c.execute("DELETE FROM skills_log WHERE person_id=%s", (karim_id,))
        c.execute("DELETE FROM persons WHERE id=%s", (karim_id,))


def test_person_analysis_levels_each_technician():
    """L'analyse des competences est par PERSONNE : niveau 1..5 construit sur l'activite reelle de
    chacun (interventions support + tickets), sans axe categorie ; une personne sans activite n'a
    pas de niveau."""
    with db.connect() as c:
        c.execute("INSERT INTO support (person_id, category, title, reported_at, resolved_at, created_at)"
                  " VALUES (2,'reseau','S1', now(), now(), now() - interval '1 h'),"
                  "        (2,'reseau','S2', now(), NULL, now() - interval '30 min'),"
                  "        (2,'serveur','S3', now(), now(), now() - interval '15 min')")
        c.execute("INSERT INTO kpi_raw (source, external_id, person_id, category, opened_at, taken_at, solved_at)"
                  " VALUES ('test','s1',2,'reseau', now() - interval '3 h', now() - interval '2 h', now() - interval '1 h'),"
                  "        ('test','s2',2,'reseau', now() - interval '4 h', NULL, NULL)")
    rows = {r["name"]: r for r in db.query("SELECT * FROM v_person_analysis")}
    priya = rows["Priya"]
    # 3 interventions support + 1 ticket du seed de tests + 2 tickets poses ici = 6 cas, 4 traites
    assert priya["interventions"] == 3 and priya["tickets"] == 3 and priya["total"] == 6
    assert priya["traites"] == 4 and float(priya["succes_pct"]) == 66.7
    assert 2000 < priya["mttd_s"] < 3400               # ~45 min de reaction, toutes sources melangees
    assert 1200 < priya["mttr_s"] < 2400
    # niveau 1..5 par personne supprime : la realisation des projets devient la competences mesuree
    assert 0 <= float(priya["sla_pct"]) <= 100 and isinstance(priya["progression"], bool)
    # la base contient au moins l'affectation du seed (personne 2) ; d'autres tests laissent
    # parfois une affectation supplementaire (ex. 'other'), d'ou un minimum, pas un nombre exact.
    assert priya.get("projets", 0) >= 1 and priya.get("proj_done", 0) == 0
    assert float(priya.get("proj_pct", 0)) == 0.0
    assert rows["Marc"]["total"] == 0 and rows["Marc"].get("niveau") is None
    # la colonne niveau et ses derives ont disparu de l'analyse
    assert "niveau" not in priya
    with db.connect() as c:                             # nettoyage : on garde le seed
        c.execute("DELETE FROM support")
        c.execute("DELETE FROM kpi_raw WHERE external_id IN ('s1','s2')")


# ------------------------------------------------------------------ taxonomie / SLA

def test_taxonomy_is_gated_to_manager(client):
    """L'onglet Categorie est reserve au manager (data.manage) : un tech n'y touche pas."""
    assert client.get("/categories", auth=BASIC_TECH).status_code == 403
    assert client.get("/categories", auth=BASIC_MGR).status_code == 200
    assert client.post("/categories", follow_redirects=False, auth=BASIC_TECH,
                       data={"type": "Intervention", "classe": "Panne", "niveau": "N1",
                             "sla_minutes": "30"}).status_code == 403
    r = client.post("/categories", auth=BASIC_MGR, follow_redirects=False,
                    data={"type": "Intervention", "classe": "Coupure reseau", "niveau": "N1",
                          "sla_minutes": "30"})
    assert r.status_code == 303 and "SLA" in r.headers["location"]
    row = db.one("SELECT * FROM categories WHERE classe='Coupure reseau'")
    assert row["niveau"] == "N1" and row["sla_minutes"] == 30
    assert db.one("SELECT count(*) AS n FROM audit_log WHERE entity='category'")["n"] == 1
    with db.connect() as c:
        c.execute("DELETE FROM categories WHERE classe='Coupure reseau'")


def test_taxonomy_upsert_recalibrates_and_validates():
    """Re-saisir (Type, Classe, Niveau) recalibre le SLA de la ligne existante ; refus des gabegies."""
    base = db.one("SELECT sla_minutes FROM categories WHERE key='serveur-n2'")["sla_minutes"]
    services.create_category({"id": 1, "role": "manager", "email": "m@x.fr"}, "test",
                             {"type": "Administration", "classe": "Serveur / stockage",
                              "niveau": "N2", "sla_minutes": 75})
    assert db.one("SELECT sla_minutes FROM categories WHERE key='serveur-n2'")["sla_minutes"] == 75
    services.create_category({"id": 1, "role": "manager", "email": "m@x.fr"}, "test",
                             {"type": "Administration", "classe": "Serveur / stockage",
                              "niveau": "N2", "sla_minutes": base})
    assert db.one("SELECT sla_minutes FROM categories WHERE key='serveur-n2'")["sla_minutes"] == base
    for d in ({"type": "Nawak", "niveau": "N1", "sla_minutes": "30"},
              {"type": "Administration", "niveau": "N0", "sla_minutes": "30"},
              {"type": "Administration", "niveau": "N1", "sla_minutes": "0"},
              {"type": "Administration", "niveau": "N1", "sla_minutes": "oui"}):
        with pytest.raises(ValueError):
            services.create_category({"id": 1, "role": "manager", "email": "m@x.fr"}, "test", d)


def test_taxonomy_delete_refused_while_referenced(client):
    """Une classe encore utilisee par des fiches ne se supprime pas (FK -> 400) ; sinon si."""
    services.create_support({"id": 1, "role": "manager", "email": "m@x.fr"}, "test", dict(SUPPORT_JSON))
    r = client.post("/categories/reseau/delete", auth=BASIC_MGR, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
    r = client.post("/categories/application-n3/delete", auth=BASIC_MGR, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert db.one("SELECT count(*) AS n FROM categories WHERE key='application-n3'")["n"] == 0
    with db.connect() as c: c.execute("DELETE FROM support")


def test_support_status_follows_resolution():
    """Une fiche resolue est forcement 'done' ; 'done' sans resolution est refuse."""
    # fiche resolue -> statut auto 'done'
    r = services.create_support({"id": 1, "role": "manager", "email": "m@x.fr"}, "test",
                                 dict(SUPPORT_JSON))
    # SUPPORT_JSON contient resolved_at => doit etre 'done'
    assert db.one("SELECT status FROM support WHERE id=%s", (r["id"],))["status"] == "done"
    # fiche terminee sans resolution -> refusee
    with pytest.raises(ValueError) as exc:
        services.create_support({"id": 1, "role": "manager", "email": "m@x.fr"}, "test",
                                {**dict(SUPPORT_JSON), "resolved_at": None, "status": "done"})
    assert "exige" in str(exc.value) or "Terminee" in str(exc.value)
    with db.connect() as c:
        # nettoyage des lignes ajoutees par ce test (le seed n'en ajoute pas)
        c.execute("DELETE FROM support WHERE title='Panne Internet globale'")


def test_support_sla_status_follows_classe_niveau():
    """Le SLA d'une fiche se lit sur (classe, niveau) : vert si le MTTR tient, rouge sinon."""
    with db.connect() as c:
        c.execute("INSERT INTO support (person_id, category, niveau, title, reported_at, resolved_at)"
                  " VALUES (2,'serveur','N1','SLA tenu (20 min)', now() - interval '2 h',"
                  " now() - interval '100 min'),"
                  "        (2,'serveur','N2','SLA depasse', now() - interval '2 h',"
                  " now() - interval '30 min')")
    ok = db.one("SELECT sla_status, mttr_s FROM v_support WHERE title='SLA tenu (20 min)'")
    assert ok["sla_status"] == "ok" and abs(ok["mttr_s"] - 1200) < 300
    bad = db.one("SELECT sla_status FROM v_support WHERE title='SLA depasse'")
    assert bad["sla_status"] == "depasse"                      # N2 = 60 min < 90 min de MTTR
    with db.connect() as c: c.execute("DELETE FROM support")


def test_dashboard_renders_and_shows_activity_bars(client):
    r = client.get("/competences", auth=BASIC_MGR)
    assert r.status_code == 200 and "Dashboard compétence" in r.text and "Suivi des competences" in r.text
    # /dashboard n'existe plus en tant qu'ecran : il renvoie au dashboard competence fusionne
    rd = client.get("/dashboard", auth=BASIC_MGR, follow_redirects=False)
    assert rd.status_code == 303 and rd.headers["location"] == "/competences"


def test_support_form_at_bottom(client):
    """"Enregistrer une intervention" est positionne apres la liste des interventions."""
    t = client.get("/support", auth=BASIC_MGR).text
    assert t.index("Interventions") < t.index("Enregistrer une intervention")


def test_support_escalade_status_counted_in_analysis(client):
    """« Bloque — Escalade » : statut ouvert, visible, compte comme non solde (jamais une reussite)
    et signale la difficulte qui necessite une escalade."""
    before = db.one("SELECT traites, total, escalades FROM v_person_analysis WHERE name='Priya'")
    r = client.post("/api/support", auth=BASIC_MGR,
                    json={"person_id": 2, "category": "reseau", "title": "Fibre a escalader",
                          "reported_at": "2026-09-20T09:00", "status": "escalade"})
    assert r.status_code == 201 and r.json()["status"] == "escalade"
    assert "Escalade" in client.get("/support", auth=BASIC_MGR).text
    assert "escalade" in client.get("/r/support/new", auth=BASIC_MGR).text
    a = db.one("SELECT traites, total, escalades FROM v_person_analysis WHERE name='Priya'")
    assert a["total"] == before["total"] + 1 and a["traites"] == before["traites"]
    assert a["escalades"] == before["escalades"] + 1
    with db.connect() as c:
        c.execute("DELETE FROM support WHERE title='Fibre a escalader'")


def test_analysis_period_filter(client):
    """Les analyses (tableau, graphiques, API) se filtrent par date / semaine / mois / annee."""
    with db.connect() as c:
        c.execute("INSERT INTO support (person_id, category, title, reported_at, resolved_at)"
                  " VALUES (2,'reseau','Vieille panne', now() - interval '2 years', now() - interval '2 years')")

    def priya_total(params):
        items = client.get("/api/competences/analyse", auth=BASIC_MGR, params=params).json()["items"]
        return next(int(r["total"]) for r in items if r["name"] == "Priya")

    all_time = priya_total({})
    assert all_time == priya_total({"period": "year"}) + 1      # la vieille panne sort du cadre annuel
    assert priya_total({"period": "month"}) == priya_total({"period": "week"}) == all_time - 1
    old = db.one("SELECT reported_at::date AS d FROM support WHERE title='Vieille panne'")["d"]
    assert priya_total({"period": "date", "d": str(old)}) == 1  # seul cas de ce jour-la
    r = client.get("/competences?period=month", auth=BASIC_MGR)
    assert r.status_code == 200 and 'value="month" selected' in r.text
    with db.connect() as c:
        c.execute("DELETE FROM support WHERE title='Vieille panne'")


def test_ticket_sla_synced_on_category_screen(client):
    """Ticketing Result : le SLA par categorie suit la Categorie (arborescence) — seuils vert/rouge
    (classe, niveau), part des tickets resolus dans le delai."""
    with db.connect() as c:
        c.execute("INSERT INTO kpi_raw (source, external_id, person_id, category, niveau,"
                  " opened_at, taken_at, solved_at)"
                  " VALUES ('test','c1',2,'Reseau','N1', now() - interval '4 h',"
                  " now() - interval '3.8 h', now() - interval '3.6 h'),"
                  "        ('test','c2',2,'Reseau','N1', now() - interval '4 h',"
                  " now() - interval '3.8 h', now() - interval '2 h')")
    row = db.one("SELECT * FROM v_kpi_by_category WHERE category='Reseau'")
    assert row["classe"] == "Reseau" and row["sla_n"] == 2 and row["sla_ok"] == 1
    assert float(row["sla_pct"]) == 50.0        # c1 : 12 min <= SLA 30 min ; c2 : 108 min depasse
    assert "SLA tenu" in client.get("/kpi", auth=BASIC_MGR).text
    with db.connect() as c:
        c.execute("DELETE FROM kpi_raw WHERE external_id IN ('c1','c2')")


def test_writes_notify_the_competences_channel(client):
    """Synchronisation dynamique : chaque ecriture sur une table source notifie le canal
    'competences' (le Dashboard competence ecoute ce canal en SSE et se recharge)."""
    listen = db.connect()
    listen.execute("LISTEN competences")
    with db.connect() as c:
        c.execute("INSERT INTO support (person_id, category, title, reported_at)"
                  " VALUES (2,'reseau','NOTIFY test', now())")
        c.execute("DELETE FROM support WHERE title='NOTIFY test'")
    payloads = [n.payload for n in listen.notifies(timeout=2)]
    listen.close()
    assert payloads.count("support") == 2
    # le canal SSE est protege comme le reste du HTML (le navigateur arrive via le cookie de session)
    r = client.get("/events", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=/events")
    assert "/events" in client.get("/competences", auth=BASIC_MGR).text   # l'ecran ecoute le canal


def test_glpi_accented_category_shares_support_sla(client):
    """Ticketing ET support partagent la MEME base de connaissance : un ticket GLPI « Réseau »
    (accentue) rejoint la classe « Reseau » de la grille et recoit son SLA (vert/rouge), comme une
    fiche support de la meme classe — cat_norm neutralise l'accent au rapprochement."""
    assert db.one("SELECT classe FROM categories WHERE niveau='N1' AND cat_norm(classe)='reseau'")["classe"] == "Reseau"
    with db.connect() as c:
        c.execute("INSERT INTO kpi_raw (source, external_id, person_id, category, niveau,"
                  " opened_at, taken_at, solved_at)"
                  " VALUES ('glpi','acc1',2,'Réseau','N1', now() - interval '4 h',"
                  " now() - interval '3.8 h', now() - interval '3.6 h'),"      # 12 min : tenu
                  "        ('glpi','acc2',2,'Réseau','N1', now() - interval '4 h',"
                  " now() - interval '3.8 h', now() - interval '1 h')")         # 168 min : depasse
    t = db.one("SELECT classe, sla_minutes, sla_status FROM v_tickets WHERE external_id='acc1'")
    assert t["classe"] == "Reseau" and t["sla_minutes"] == 30 and t["sla_status"] == "ok"
    assert db.one("SELECT sla_status FROM v_tickets WHERE external_id='acc2'")["sla_status"] == "depasse"
    # l'analyse par personne compte ce SLA reseau, toutes sources (support + ticket accentue)
    a = db.one("SELECT sla_n FROM v_person_analysis WHERE name='Priya'")
    assert a["sla_n"] >= 2
    with db.connect() as c:
        c.execute("DELETE FROM kpi_raw WHERE external_id IN ('acc1','acc2')")


def test_open_ticket_shows_live_sla_on_kpi_screen(client):
    """Un ticket GLPI synchronise et NON resolu doit quand meme afficher la validation SLA
    (avant : « — », le SLA semblait absent des tickets synchronises). Dans les temps = bleu,
    hors delai = rouge ; resolu = vert/rouge selon le MTTR."""
    with db.connect() as c:
        c.execute("INSERT INTO kpi_raw (source, external_id, person_id, category, niveau,"
                  " opened_at, taken_at, solved_at, status)"
                  " VALUES ('test','o1',2,'reseau','N1', now() - interval '2 h',"
                  " now() - interval '10 min', NULL, 'encours'),"
                  "        ('test','o2',2,'reseau','N1', now() - interval '6 h',"
                  " now() - interval '4 h', NULL, 'encours')")
    assert db.one("SELECT sla_status FROM v_tickets WHERE external_id='o1'")["sla_status"] == "dans_delai"
    assert db.one("SELECT sla_status FROM v_tickets WHERE external_id='o2'")["sla_status"] == "depasse"
    t = client.get("/kpi", auth=BASIC_MGR).text
    assert "dans les temps" in t and "sla depasse" in t
    with db.connect() as c:
        c.execute("DELETE FROM kpi_raw WHERE external_id IN ('o1','o2')")


def test_uncategorized_glpi_ticket_still_gets_sla(client):
    """Un ticket GLPI sans categorie (repli import 'incident'/'request') ne doit PAS rester « — » :
    les classes fourre-tout Incident / Demande portent un SLA, et le glpi.normalize y replie
    desormais directement."""
    with db.connect() as c:
        c.execute("INSERT INTO kpi_raw (source, external_id, person_id, category, niveau,"
                  " opened_at, taken_at, solved_at)"
                  " VALUES ('test','u1',2,'Incident','N1', now() - interval '3 h',"
                  " now() - interval '2.8 h', now() - interval '1 h')")
    row = db.one("SELECT classe, sla_minutes, sla_status FROM v_tickets WHERE external_id='u1'")
    assert row["classe"] == "Incident" and row["sla_minutes"] == 30 and row["sla_status"] == "depasse"
    # le repli d'import pointe sur ces classes (plus 'incident'/'request' hors grille)
    t = glpi.normalize({"id": "x", "name": "sans categorie", "type": 2,
                        "date_creation": "2026-09-01 10:00:00"}, db.connect())
    assert t[3] in ("Demande", "Incident")
    with db.connect() as c:
        c.execute("DELETE FROM kpi_raw WHERE external_id='u1'")


def test_glpi_import_does_not_duplicate_accented_class():
    """Importer la categorie GLPI « Réseau » ne cree PAS une deuxieme classe : elle rejoint la
    classe existante « Reseau » (base de connaissance partagee support/ticketing)."""
    before = db.one("SELECT count(*) n FROM categories WHERE cat_norm(classe)='reseau'")["n"]
    n = glpi.import_categories([{"name": "Réseau", "color": "#123456"}])
    assert n >= 1
    rows = db.query("SELECT classe, couleur FROM categories WHERE cat_norm(classe)='reseau'")
    assert len(rows) == before, [r["classe"] for r in rows]            # toujours la meme classe, pas de doublon
    assert rows[0]["classe"] == "Reseau"
    assert all(r["couleur"] == "#123456" for r in rows)                # la couleur GLPI rejoint l'existant
    with db.connect() as c:
        c.execute("UPDATE categories SET couleur = '#2f5d8a' WHERE cat_norm(classe)='reseau'")


def test_competences_person_filter(client):
    """Le Dashboard competence se filtre par personne via un champ RECHERCHE (input+datalist,
    toutes les personnes actives, sous-chaine insensible a la casse) — pas une simple liste."""
    assert len({r["name"] for r in db.query("SELECT name FROM v_person_analysis")}) >= 2
    t = client.get("/competences", auth=BASIC_MGR).text
    assert 'list="personnes"' in t and 'name="personne"' in t   # champ de recherche + datalist
    assert "<datalist" in t and 'value="Priya"' in t and 'value="Marc"' in t
    one = client.get("/competences?personne=pri", auth=BASIC_MGR)
    assert one.status_code == 200 and 'value="pri"' in one.text
    items = client.get("/api/competences/analyse", auth=BASIC_MGR, params={"personne": "PRI"}).json()["items"]
    assert [r["name"] for r in items] == ["Priya"]              # casse ignoree, sous-chaine suffit
    assert client.get("/api/competences/analyse", auth=BASIC_MGR,
                      params={"personne": "zzz"}).json()["items"] == []


def test_glpi_categories_import_updates_taxonomy():
    """Le chemin glpi.import_categories transforme une liste de categories (feuille ' > ' epluchee)
    en lignes de la grille ; sans endpoint /Category (simu) on importe quand meme."""
    # import direct : une nouvelle classe recoit 3 niveaux (N1/N2/N3) + couleur
    n = glpi.import_categories([{"name": "Incident > Coupure fibre quartier est", "color": "#ff4400"}])
    # le chemin GLPI est epluche en feuille : "Coupure fibre quartier est"
    assert n >= 3  # N1 + N2 + N3 au minimum
    row = db.one("SELECT * FROM categories WHERE classe = %s", ("Coupure fibre quartier est",))
    assert row is not None and row["couleur"] == "#ff4400"
    # une classe deja existante garde son SLA et recoit la couleur
    # nettoyage : supprimer la nouvelle classe a tous ses niveaux
    with db.connect() as c:
        c.execute("DELETE FROM categories WHERE classe = %s", ("Coupure fibre quartier est",))


def test_ticket_sla_status_renders_on_kpi_screen(client):
    with db.connect() as c:
        c.execute("INSERT INTO kpi_raw (source, external_id, person_id, category, niveau,"
                  " opened_at, taken_at, solved_at)"
                  " VALUES ('test','t1',2,'reseau','N1', now() - interval '4 h',"
                  " now() - interval '3.8 h', now() - interval '3.6 h'),"
                  "        ('test','t2',2,'serveur','N2', now() - interval '5 h',"
                  " now() - interval '4 h', now() - interval '2 h')")
    assert db.one("SELECT sla_status FROM v_tickets WHERE external_id='t1'")["sla_status"] == "ok"
    assert db.one("SELECT sla_status FROM v_tickets WHERE external_id='t2'")["sla_status"] == "depasse"
    r = client.get("/kpi", auth=BASIC_MGR)
    assert r.status_code == 200 and "Ticketing" in r.text and "sla tenu" in r.text
    with db.connect() as c:
        c.execute("DELETE FROM kpi_raw WHERE external_id IN ('t1','t2')")


def test_partial_update_does_not_erase_unsent_fields(client):
    """Garde-fou des charges partielles (PUT de l'API, outil MCP) : un appel qui ne cite qu'un champ
    ne doit pas effacer les autres. Le formulaire, lui, envoie tout et peut vider un champ."""
    before = db.one("SELECT due_date, progress FROM assignments WHERE id=1")
    r = client.put("/api/affectations/1", auth=BASIC_MGR, json={"role": "techin", "status": "done"})
    assert r.status_code == 200 and r.json()["status"] == "done"
    after = db.one("SELECT due_date, progress FROM assignments WHERE id=1")
    assert (after["due_date"], after["progress"]) == (before["due_date"], before["progress"])


def test_mcp_analysis_parity_with_api(client):
    """Le MCP lit la MEME requete que l'ecran/API : memes chiffres, memes filtres (sous-chaine du
    nom, periode) — une porte ne peut pas devancer l'autre."""
    api = client.get("/api/competences/analyse", auth=BASIC_MGR).json()["items"]
    mcp_rows = __import__("json").loads(call("get_person_analysis").content[0].text)
    assert sorted((r["name"], r["total"]) for r in api) == sorted((r["name"], r["total"]) for r in mcp_rows)
    sub = __import__("json").loads(call("get_person_analysis", person="pri").content[0].text)
    assert [r["name"] for r in sub] == ["Priya"] == [r["name"] for r in client.get(
        "/api/competences/analyse", auth=BASIC_MGR, params={"personne": "pri"}).json()["items"]]
    vide = __import__("json").loads(
        call("get_person_analysis", since="2001-01-01", until="2001-02-01").content[0].text)
    assert vide and all(int(r["total"]) == 0 for r in vide)   # fenetre vide : tout le monde, zero activite
    assert {r["name"] for r in __import__("json").loads(
        call("list_persons").content[0].text) if r["active"]} == {r["name"] for r in api}


def test_mcp_skills_docs_match_platform():
    """La grille manuelle 1-5 n'est pas le dashboard : le MCP doit le declarer distinctement
    (get_skills/set_skill = grille sans ecran, get_person_analysis = l'analyse du dashboard)."""
    async def go():
        from fastmcp import Client
        async with Client(mcp) as c:
            return {t.name: (t.description or "") for t in await c.list_tools()}
    ds = asyncio.run(go())
    assert "grille" in ds["get_skills"].lower() and "dashboard" in ds["get_skills"].lower()
    assert "grille" in ds["set_skill"].lower()


def test_mcp_support_and_analysis_tools():
    """le MCP reste compatible : lire les interventions, en creer (avec la porte), lire l'analyse."""
    tools = asyncio.run(_list_tools())
    assert {"list_support", "log_support", "get_person_analysis"} <= tools
    with pytest.raises(Exception) as err:
        call("log_support", person="Priya", category="reseau", title="Fibre en panne",
             reported_at="2026-09-20T09:00")
    assert "MCP_WRITE" in str(err.value)
    os.environ["MCP_WRITE"] = "1"
    os.environ["MCP_ACTOR_EMAIL"] = "manager@example.com"
    try:
        out = call("log_support", person="Priya", category="serveur", title="Disque NAS plein",
                   reported_at="2026-09-25T08:00", resolved_at="2026-09-25T12:00",
                   solution="Purge et volume").content[0].text
        assert "Disque NAS plein" in out
        assert db.one("SELECT title FROM support ORDER BY id DESC LIMIT 1")["title"] == "Disque NAS plein"
        listed = call("list_support", category="serveur").content[0].text
        assert "Disque NAS plein" in listed
        analysed = call("get_person_analysis", person="Priya").content[0].text
        assert "Priya" in analysed and ("projets" in analysed or "proj_pct" in analysed)
    finally:
        os.environ.pop("MCP_WRITE", None)
        with db.connect() as c:
            c.execute("DELETE FROM support")
