"""FastAPI : les ecrans web (Equipe / Competences / Projets / Support / Kanban / KPI / Roles)
+ l'API JSON + le montage du serveur MCP.

Ecrans et API partagent un seul moteur CRUD (RESOURCES) et une seule couche d'ecriture (app.services).
"""
import os
from datetime import datetime, timedelta

import httpx
from contextlib import asynccontextmanager
from urllib.parse import quote, urlparse

import psycopg
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db, glpi, services
from .services import period_range
from .auth import (COOKIE, Denied, actor_from_token, close_session, current_user, needs, open_session, perms,
                   person_by_email, verify_password)

HERE = os.path.dirname(__file__)

# ------------------------------------------------------------------ moteur CRUD
# fields: (nom, label, type, obligatoire, options) ; options = liste (valeur, label) ou "ref:<sql>".
REF = "ref:"

RESOURCES = {
    "personnes": {
        "title": "Personne", "table": "persons",
        "sql": "SELECT id, name, title, role, glpi_id, email, active FROM persons ORDER BY active DESC, name",
        "cols": [("name", "Nom"), ("title", "Poste"), ("role", "Role"), ("glpi_id", "ID GLPI"),
                 ("email", "Email"), ("active", "Actif")],
        "fields": [("name", "Nom", "text", True, None), ("email", "Email", "text", True, None),
                   ("title", "Poste", "text", False, None),
                   ("glpi_id", "ID GLPI (technicien)", "number", False, None),
                   ("role", "Role", "select", True, REF + "SELECT name AS id, name AS name FROM roles ORDER BY name"),
                   ("password", "Mot de passe", "password", False, None)],
        "create": services.create_person, "update": services.update_person, "delete": services.delete_person,
    },
    "competences": {
        "title": "Competence", "table": "skills",
        "sql": "SELECT s.id, pe.name AS personne, cat.classe AS category_label, s.category, s.level,"
               " s.updated_at, s.person_id FROM skills s JOIN persons pe ON pe.id = s.person_id"
               " JOIN categories cat ON cat.key = s.category ORDER BY pe.name, cat.type, cat.classe",
        "cols": [("personne", "Personne"), ("category_label", "Base de connaissance"), ("level", "Niveau"),
                 ("updated_at", "Maj", "dt")],
        "fields": [("person_id", "Personne", "select", True, REF + "SELECT id, name FROM persons ORDER BY name"),
                   ("category", "Base de connaissance", "select", True,
                    REF + "SELECT c.key AS id, c.type || ' — ' || c.classe AS name"
                          " FROM categories c WHERE c.niveau='N1' ORDER BY c.type, c.classe"),
                   ("level", "Niveau (1-5)", "number", True, None),
                   ("note", "Note (historique)", "textarea", False, None)],
        "create": services.upsert_skill, "update": services.upsert_skill, "delete": None,
    },
    "support": {
        "title": "Intervention", "table": "support",
        "sql": "SELECT * FROM v_support ORDER BY reported_at DESC",
        "cols": [("reported_at", "Signale", "dt"), ("person", "Technicien"), ("type", "Type"),
                 ("category_label", "Classe"), ("niveau", "Niveau"), ("mttd_s", "MTTD", "dur"),
                 ("mttr_s", "MTTR", "dur"), ("sla_status", "SLA", "sla"), ("status", "Statut", "st"),
                 ("en_cours", "Etat"), ("solution", "Solution")],
        "fields": [("person_id", "Technicien", "select", True, REF + "SELECT id, name FROM persons ORDER BY name"),
                   ("category", "Classe", "select", True,
                    REF + "SELECT c.key AS id, c.type || ' — ' || c.classe AS name"
                          " FROM categories c WHERE c.niveau='N1' ORDER BY c.type, c.classe"),
                   ("niveau", "Niveau de difficulte", "select", True,
                    [("N1", "N1 (base)"), ("N2", "N2"), ("N3", "N3")]),
                   ("title", "Intervention", "text", True, None),
                   ("reported_at", "Signale le (AAAA-MM-JJTHH:MM)", "datetime-local", True, None),
                   ("resolved_at", "Resolu le (vide = en cours)", "datetime-local", False, None),
                   ("status", "Statut", "select", False,
                    [("todo", "A faire"), ("in_progress", "En cours"), ("blocked", "Bloque"),
                     ("escalade", "Bloque — Escalade"), ("done", "Termine")]),
                   ("solution", "Solution / action realisee", "textarea", False, None)],
        "create": services.create_support, "update": services.update_support, "delete": services.delete_support,
    },
    "categories": {
        "title": "Categorie (arborescence)", "table": "categories",
        "sql": "SELECT type, classe, niveau, sla_minutes, couleur, key FROM categories"
               " ORDER BY type, classe, niveau",
        "cols": [("type", "Type"), ("classe", "Classe"), ("niveau", "Niveau"),
                 ("sla_minutes", "SLA (min)"), ("couleur", "Couleur", "couleur")],
        "fields": [
            ("type", "Type", "select", True,
             [("Intervention", "Intervention"), ("Administration", "Administration"),
              ("Deploiement", "Deploiement"), ("Autre", "Autre")]),
            ("classe", "Classe", "text", True, None),
            ("niveau", "Niveau", "select", True, [("N1", "N1 (base)"), ("N2", "N2"), ("N3", "N3")]),
            ("sla_minutes", "SLA (minutes)", "number", True, None),
            ("couleur", "Couleur (#rrggbb)", "text", False, None)],
    },
    "projets": {
        "title": "Projet", "table": "projects",
        "sql": "SELECT id, name, status, start_date, due_date, description, category, done_at FROM projects"
               " ORDER BY due_date NULLS LAST, name",
        "cols": [("name", "Projet"), ("status", "Statut"), ("start_date", "Debut", "day"),
                 ("due_date", "Echeance", "day"), ("description", "Description")],
        "fields": [("name", "Projet", "text", True, None),
                   ("status", "Statut", "select", True, [("planned", "Planifie"), ("running", "En cours"),
                                                          ("done", "Termine"), ("cancelled", "Annule")]),
                   ("start_date", "Debut", "date", False, None), ("due_date", "Echeance", "date", False, None),
                   ("category", "Classe (arborescence)", "select", False,
                    REF + "SELECT c.key AS id, c.type || ' — ' || c.classe AS name"
                          " FROM categories c WHERE c.niveau='N1' ORDER BY c.type, c.classe"),
                   ("done_at", "Termine le", "date", False, None),
                   ("description", "Description", "textarea", False, None)],
        "create": services.create_project, "update": services.update_project, "delete": services.delete_project,
    },
    "affectations": {
        "title": "Affectation", "table": "assignations",
        "sql": "SELECT a.id, p.name AS projet, pe.name AS personne, a.role, a.status, a.progress,"
               " a.start_date, a.due_date, a.project_id, a.person_id,"
               " (a.due_date IS NOT NULL AND a.due_date < current_date AND a.status <> 'done') AS en_retard"
               " FROM assignments a JOIN projects p ON p.id = a.project_id"
               " JOIN persons pe ON pe.id = a.person_id ORDER BY a.due_date NULLS LAST, p.name",
        "cols": [("projet", "Projet"), ("personne", "Personne"), ("role", "Role"), ("status", "Statut"),
                 ("progress", "Avancement"), ("due_date", "Echeance", "day"), ("en_retard", "En retard")],
        "fields": [("project_id", "Projet", "select", True, REF + "SELECT id, name FROM projects ORDER BY name"),
                   ("person_id", "Personne", "select", True, REF + "SELECT id, name FROM persons ORDER BY name"),
                   ("role", "Role", "text", True, None),
                   ("status", "Statut", "select", True, [("todo", "A faire"), ("in_progress", "En cours"),
                                                          ("done", "Termine"), ("blocked", "Bloque")]),
                   ("start_date", "Debut", "date", False, None), ("due_date", "Echeance", "date", False, None),
                   ("progress", "Avancement %", "number", False, None)],
        "create": services.create_assignment, "update": services.update_assignment,
        "delete": services.delete_assignment,
    },
    "objectifs": {
        "title": "Objectif", "table": "objectives",
        "sql": "SELECT o.id, pe.name AS personne, o.period, o.title, o.target, o.achieved, o.person_id"
               " FROM objectives o JOIN persons pe ON pe.id = o.person_id ORDER BY o.period DESC, pe.name",
        "cols": [("personne", "Personne"), ("period", "Periode"), ("title", "Objectif"),
                 ("target", "Cible"), ("achieved", "Atteint")],
        "fields": [("person_id", "Personne", "select", True, REF + "SELECT id, name FROM persons ORDER BY name"),
                   ("period", "Periode (ex. 2026-Q1)", "text", True, None),
                   ("title", "Objectif", "text", True, None),
                   ("target", "Cible", "number", False, None), ("achieved", "Atteint", "number", False, None)],
        "create": services.set_objective, "update": services.set_objective, "delete": services.delete_objective,
    },
}


# ------------------------------------------------------------------ application
mcp_asgi = None
try:
    from .mcp_server import mcp

    mcp_asgi = mcp.http_app(path="/", transport="streamable-http")
except Exception as exc:  # le serveur MCP est optionnel : l'app doit tourner sans
    import logging

    logging.getLogger(__name__).warning("serveur MCP indisponible (%s)", exc)


@asynccontextmanager
async def lifespan(_app):
    db.init()
    if mcp_asgi is not None:
        async with mcp_asgi.router.lifespan_context(mcp_asgi):
            yield
    else:
        yield


app = FastAPI(title="Mini-SIRH technique", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))


def dur(seconds):
    """Secondes -> '2h14' / '35min' (KPI lisibles sans unitaire SQL)."""
    if seconds is None:
        return "-"
    s = int(round(float(seconds)))
    return f"{s // 3600}h{s % 3600 // 60:02d}" if s >= 3600 else f"{s // 60}min"


templates.env.filters["dur"] = dur
# formatage par tranche : marche pour une date Postgres comme pour une chaine sortie de jsonb
templates.env.filters["dt"] = lambda v: str(v)[:16].replace("T", " ") if v else "-"
templates.env.filters["day"] = lambda v: str(v)[:10] if v else ""
# <input type="datetime-local"> attend 'AAAA-MM-JJTHH:MM' ; un datetime Postgres sort avec un espace
templates.env.filters["dtl"] = lambda v: str(v)[:16].replace(" ", "T") if v else ""
templates.env.filters["pct"] = lambda v: "-" if v is None else f"{float(v):.1f} %"
templates.env.filters["yn"] = lambda v: "" if v is None else ("oui" if v else "non")


@app.exception_handler(StarletteHTTPException)
async def http_error(request, exc):
    if request.url.path.startswith("/api"):
        # headers conserve : le 401 doit garder son defi Basic, sinon le client ne sait pas quoi faire
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    if exc.status_code == 401:
        # le navigateur ne sait pas se deconnecter d'un Basic : on le renvoie vers le formulaire
        return RedirectResponse("/login?next=" + quote(request.url.path), status_code=303)
    return templates.TemplateResponse(request, "erreur.html", {"code": exc.status_code, "message": exc.detail},
                                      status_code=exc.status_code, headers=exc.headers)


# ------------------------------------------------------------------ helpers

def guard(fn, *args, **kw):
    """Traduit les refus metier (droits, validation, contraintes) en reponses HTTP."""
    try:
        return fn(*args, **kw)
    except Denied as e:
        raise StarletteHTTPException(403, str(e))
    except ValueError as e:
        raise StarletteHTTPException(400, str(e))
    except psycopg.errors.UniqueViolation:
        raise StarletteHTTPException(409, "valeur deja utilisee (contrainte d'unicite)")
    except (psycopg.errors.ForeignKeyViolation, psycopg.errors.CheckViolation,
            psycopg.errors.NotNullViolation, psycopg.errors.InvalidTextRepresentation) as e:
        raise StarletteHTTPException(400, "contrainte : " + str(e).splitlines()[0])


def same_origin(request) -> bool:
    """Un POST de formulaire avec identifiants en cache ne doit pas venir d'un site tiers."""
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    return not origin or urlparse(origin).netloc == request.headers.get("host")


async def form_or_json(request) -> dict:
    if request.headers.get("content-type", "").startswith("application/json"):
        return await request.json()
    return dict(await request.form())


def ref_options(spec):
    if isinstance(spec, str) and spec.startswith(REF):
        return db.query(spec[len(REF):] + "")
    return spec


def page(request, name, ctx, actor):
    ctx.update(request=request, actor=actor, perms=perms(actor), err=request.query_params.get("err"),
               msg=request.query_params.get("msg"))
    return templates.TemplateResponse(request, name, ctx)


def back_to(request):
    return request.headers.get("referer") or "/"


KANBAN_COLS = [("todo", "A faire"), ("in_progress", "En cours"), ("blocked", "Bloque"), ("done", "Termine")]
KANBAN_SQL = ("SELECT a.id, a.role, a.status, a.progress, a.due_date, p.name AS project, pe.name AS person,"
              " (a.due_date IS NOT NULL AND a.due_date < current_date AND a.status <> 'done') AS overdue,"
              " COALESCE(c.couleur, '#94a3b8') AS couleur"     # le code couleur de la classe du projet
              " FROM assignments a JOIN projects p ON p.id = a.project_id"
              " JOIN persons pe ON pe.id = a.person_id"
              " LEFT JOIN categories c ON c.key = p.category"
              " ORDER BY a.due_date NULLS LAST, p.name")


# ------------------------------------------------------------------ session

def safe_next(target):
    """Revenu sur un chemin interne seulement : jamais une URL absolue envoyee par la query string."""
    return target if target and target.startswith("/") and not target.startswith("//") else "/equipe"


def set_cookie(response, token):
    # ponytail: secure=False car l'app tourne en HTTP local ; passer a True derriere un reverse proxy TLS.
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=12 * 3600)
    return response


@app.get("/login", response_class=HTMLResponse)
def screen_login(request: Request, next: str = "/equipe", err: str = ""):
    return templates.TemplateResponse(request, "login.html", {"next": safe_next(next), "err": err})


@app.post("/login")
async def do_login(request: Request):
    form = await form_or_json(request)
    actor = person_by_email(str(form.get("email") or ""))
    if not actor or not verify_password(str(form.get("password") or ""), actor["password_hash"]):
        return RedirectResponse("/login?err=" + quote("identifiants invalides"), status_code=303)
    with db.connect() as conn:
        services.audit(conn, "login", actor, "open", "session", actor["id"],
                       {"ip": request.client.host if request.client else None})
    return set_cookie(RedirectResponse(safe_next(form.get("next")), status_code=303), open_session(actor["id"]))


@app.get("/logout")
def do_logout(request: Request):
    """Le cookie porte la session : la supprimer, c'est vraiment se deconnecter (contrairement au Basic)."""
    actor = actor_from_token(request.cookies.get(COOKIE)) if request.cookies.get(COOKIE) else None
    close_session(request.cookies.get(COOKIE))
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie(COOKIE)
    if actor:
        with db.connect() as conn:
            services.audit(conn, "logout", actor, "close", "session", actor["id"], {})
    return r


# ------------------------------------------------------------------ roles et permissions

@app.get("/roles", response_class=HTMLResponse)
def screen_roles(request: Request, actor=Depends(current_user)):
    return page(request, "roles.html", {
        "roles": db.query("SELECT r.name, r.description, count(p.id) AS users,"
                          " array_remove(array_agg(DISTINCT rp.perm), NULL) AS perms"
                          " FROM roles r"
                          " LEFT JOIN persons p ON p.role = r.name"
                          " LEFT JOIN role_permissions rp ON rp.role = r.name"
                          " GROUP BY r.name, r.description ORDER BY r.name"),
        "all_perms": db.query("SELECT key, label FROM permissions ORDER BY key"),
    }, actor)


@app.post("/roles/new")
async def role_new(request: Request, actor=Depends(current_user)):
    if not same_origin(request):
        raise StarletteHTTPException(403, "origine refusee")
    try:
        guard(services.create_role, actor, "dashboard", await form_or_json(request))
    except StarletteHTTPException as e:
        return RedirectResponse(f"/roles?err={quote(str(e.detail))}", status_code=303)
    return RedirectResponse("/roles?msg=role cree", status_code=303)


@app.post("/roles/{role}/perms")
async def role_perms(request: Request, role: str, actor=Depends(current_user)):
    if not same_origin(request):
        raise StarletteHTTPException(403, "origine refusee")
    granted = (await request.form()).getlist("perm")   # une case cochee = une permission accordee
    try:
        guard(services.set_role_permissions, actor, "dashboard", role, granted)
    except StarletteHTTPException as e:
        return RedirectResponse(f"/roles?err={quote(str(e.detail))}", status_code=303)
    return RedirectResponse(f"/roles?msg=permissions de {role} mises a jour", status_code=303)


@app.post("/roles/{role}/delete")
async def role_delete(request: Request, role: str, actor=Depends(current_user)):
    if not same_origin(request):
        raise StarletteHTTPException(403, "origine refusee")
    try:
        guard(services.delete_role, actor, "dashboard", role)
    except StarletteHTTPException as e:
        return RedirectResponse(f"/roles?err={quote(str(e.detail))}", status_code=303)
    return RedirectResponse("/roles?msg=role supprime", status_code=303)


# ------------------------------------------------------------------ ecrans

@app.get("/", response_class=HTMLResponse)
def screen_home(request: Request, actor=Depends(current_user)):
    return page(request, "equipe.html", {"people": db.query(RESOURCES["personnes"]["sql"])}, actor)


@app.get("/equipe", response_class=HTMLResponse)
def screen_team(request: Request, actor=Depends(current_user)):
    return page(request, "equipe.html", {"people": db.query(RESOURCES["personnes"]["sql"])}, actor)


@app.get("/personnes/{person_id}", response_class=HTMLResponse)
def screen_person(request: Request, person_id: int, actor=Depends(current_user)):
    hist = db.one("SELECT * FROM v_person_history WHERE person_id = %s", (person_id,))
    if not hist:
        raise StarletteHTTPException(404, "personne introuvable")
    return page(request, "personne.html", {
        "hist": hist,
        "assignments": hist["assignments"],
        "skills_log": hist["skills"],
        "notes": hist["notes"],
        "support": hist["support"],
        "kpi": db.one("SELECT * FROM v_kpi_by_person WHERE person_id = %s", (person_id,)),
        "objectives": db.query("SELECT * FROM objectives WHERE person_id = %s ORDER BY period DESC", (person_id,)),
        "glpi_url": os.environ.get("GLPI_URL", ""),
        "assign_fields": [("assignment_id", "Affectation", "select", True,
                           db.query("SELECT a.id, p.name || ' (' || a.role || ')' AS name FROM assignments a"
                                    " JOIN projects p ON p.id = a.project_id"
                                    " WHERE a.person_id = %s ORDER BY a.id", (person_id,)))],
    }, actor)


@app.get("/support", response_class=HTMLResponse)
def screen_support(request: Request, category: str | None = None, actor=Depends(current_user)):
    """Interventions techniques saisies a la main : ni projet, ni ticket de l'outil de tickets."""
    where = "" if not category else " WHERE category = %s"
    params = (category,) if category else ()
    return page(request, "support.html", {
        "rows": db.query("SELECT * FROM v_support" + where + " ORDER BY reported_at DESC LIMIT 200", params),
        "kpi": db.one("SELECT count(*) AS total,"
                      " count(*) FILTER (WHERE resolved_at IS NOT NULL) AS resolues,"
                      " avg(EXTRACT(EPOCH FROM (resolved_at - reported_at))) FILTER"
                      " (WHERE resolved_at IS NOT NULL) AS mttr_s,"
                      " avg(EXTRACT(EPOCH FROM (reported_at - created_at))) FILTER"
                      " (WHERE reported_at >= created_at) AS mttd_s FROM support"),
        "by_person": db.query(
            "SELECT pe.name AS personne, count(s.id) AS interventions,"
            " count(s.id) FILTER (WHERE s.resolved_at IS NOT NULL) AS resolues,"
            " round(100.0 * count(s.id) FILTER (WHERE s.resolved_at IS NOT NULL)"
            "     / nullif(count(s.id), 0), 1) AS succes_pct,"
            " avg(EXTRACT(EPOCH FROM (s.reported_at - s.created_at))) FILTER"
            "     (WHERE s.reported_at >= s.created_at) AS mttd_s,"
            " avg(EXTRACT(EPOCH FROM (s.resolved_at - s.reported_at))) FILTER"
            "     (WHERE s.resolved_at IS NOT NULL) AS mttr_s"
            " FROM persons pe LEFT JOIN support s ON s.person_id = pe.id"
            " WHERE pe.active GROUP BY pe.id, pe.name HAVING count(s.id) > 0"
            " ORDER BY count(s.id) DESC, pe.name"),
        "category": category,
        "categories": db.query("SELECT c.key, c.type || ' — ' || c.classe AS label"
                               " FROM categories c WHERE c.niveau='N1' ORDER BY c.type, c.classe"),
        "fields": _options(RESOURCES["support"]),
        # l'horodatage du jour par defaut : une fiche saisie en temps reel est la seule ou le MTTD
        # est mesurable (remplir le champ a la minute evite de le laisser vide)
        "now": datetime.now().replace(second=0, microsecond=0),
    }, actor)


@app.get("/categories", response_class=HTMLResponse)
def screen_categories(request: Request, actor=Depends(needs("data.manage"))):
    """Arborescence des taches, geree par le manager : Type -> Classe -> Niveau -> SLA."""
    used = {r["category"] for r in db.query("SELECT DISTINCT category FROM support")}
    return page(request, "categories.html", {
        "rows": db.query(RESOURCES["categories"]["sql"]),
        "fields": _options(RESOURCES["categories"]),
        "used": used,
    }, actor)


@app.post("/categories")
async def category_upsert(request: Request, actor=Depends(needs("data.manage"))):
    """Une ligne (Type, Classe, Niveau, SLA) ; re-saisir une combinaison existante recalibre son SLA."""
    if not same_origin(request):
        raise StarletteHTTPException(403, "origine refusee")
    try:
        row = guard(services.create_category, actor, "categorie", await form_or_json(request))
    except StarletteHTTPException as e:
        return RedirectResponse("/categories?err=" + quote(str(e.detail)), status_code=303)
    return RedirectResponse(f"/categories?msg={quote(
        f'SLA {row["niveau"]} de la classe « {row["classe"]} » enregistre ({row["sla_minutes"]} min)')}",
        status_code=303)


@app.post("/categories/{key}/delete")
def category_delete(request: Request, key: str, actor=Depends(needs("data.manage"))):
    if not same_origin(request):
        raise StarletteHTTPException(403, "origine refusee")
    try:
        guard(services.delete_category, actor, "categorie", key)
    except StarletteHTTPException as e:
        return RedirectResponse("/categories?err=" + quote(str(e.detail)), status_code=303)
    return RedirectResponse("/categories?msg=" + quote("ligne retiree de la grille"), status_code=303)


@app.get("/competences", response_class=HTMLResponse)
def screen_skills(request: Request, period: str = "all", d: str | None = None,
                  personne: str | None = None, actor=Depends(current_user)):
    """Dashboard competence interactif : le suivi par personne (f_person_analysis) et ses
    graphiques Chart.js lisent la MEME requete — support (statuts + SLA), tickets GLPI (SLA)
    et projets — filtrable par date / semaine / mois / annee ET par personne."""
    p_from, p_to = period_range(period, d)
    rows = db.query(
        "SELECT person_id, name, total, interventions, tickets, traites, succes_pct, sla_pct, sla_n,"
        "       mttd_s, mttr_s, projets, proj_done, proj_pct, diff_max, progression,"
        "       escalades, en_cours"
        " FROM f_person_analysis(%s, %s) ORDER BY total DESC, name", (p_from, p_to))
    # recherche par personne : sous-chaine insensible a la casse (le champ HTML est une <input list>
    # avec datalist — autocomplete natif, utilisable meme a 100 personnes, aucun JS ajoute)
    if personne:
        q = personne.lower()
        analysis = [r for r in rows if q in r["name"].lower()]
        person_ids = [r["person_id"] for r in analysis]
    else:
        analysis, person_ids = rows, None
    classes = db.query(
        "SELECT c.classe, COALESCE(c.couleur, '#94a3b8') AS couleur, count(*) AS n"
        " FROM (SELECT category AS cat, reported_at AS at, person_id FROM support"
        "       UNION ALL SELECT category, opened_at, person_id FROM kpi_raw"
        "       UNION ALL SELECT category, COALESCE(done_at, start_date), NULL FROM projects"
        "         WHERE category IS NOT NULL) t"
        " JOIN categories c ON c.niveau = 'N1'"
        "   AND (lower(c.key) = lower(t.cat) OR cat_norm(c.classe) = cat_norm(t.cat))"
        " WHERE (%s::timestamptz IS NULL OR t.at >= %s) AND (%s::timestamptz IS NULL OR t.at < %s)"
        "   AND (%s::int[] IS NULL OR t.person_id = ANY(%s))"
        " GROUP BY c.classe, c.couleur ORDER BY n DESC, c.classe",
        (p_from, p_from, p_to, p_to, person_ids, person_ids))
    # les cartes se calculent sur les MEMES lignes que le tableau : elles ne peuvent pas diverger
    stats = {
        "taches": sum(int(r["interventions"] or 0) + int(r["tickets"] or 0) for r in analysis),
        "projets_termines": sum(int(r["proj_done"] or 0) for r in analysis),
        "effectif": len(analysis),
        "classes": db.one("SELECT count(*) AS n FROM categories WHERE niveau = 'N1'")["n"],
    }
    num = lambda v: float(v) if v is not None else 0
    chart = {  # memes lignes que la table : le graphique et le tableau lisent la meme requete
        "names": [r["name"] for r in analysis],
        "support": [r["interventions"] for r in analysis],
        "tickets": [r["tickets"] for r in analysis],
        "sla": [num(r["sla_pct"]) for r in analysis],
        "proj": [num(r["proj_pct"]) for r in analysis],
        "escalades": [r["escalades"] for r in analysis],
        "classes": [r["classe"] for r in classes],
        "cls_n": [r["n"] for r in classes],
        "cls_colors": [r["couleur"] for r in classes],
    }
    return page(request, "competences.html", {
        "analysis": analysis, "classes": classes, "stats": stats, "chart": chart,
        "period": period, "d": d or "", "personne": personne or "",
        # datalist de recherche : toutes les personnes actives, pas seulement celles de la periode
        "toutes_personnes": [r["name"] for r in db.query(
            "SELECT name FROM persons WHERE active ORDER BY name")],
    }, actor)


@app.get("/dashboard", response_class=HTMLResponse)
def screen_dashboard(request: Request, actor=Depends(current_user)):
    return RedirectResponse("/competences", status_code=303)


@app.get("/events")
async def events(request: Request, actor=Depends(current_user)):
    """SSE : les triggers competences_sync NOTIFIENT a chaque ecriture (support, tickets,
    affectations, competences, objectifs, categories) ; le Dashboard competence ecoute ce canal
    et se recharge des qu'une donnee change, d'ou qu'elle vienne (UI, API, MCP, import GLPI)."""
    async def stream():
        conn = await db.async_listen(db.DSN)
        await conn.execute("LISTEN competences")
        try:
            while not await request.is_disconnected():
                async for n in conn.notifies(timeout=15):   # clot l'iteration apres 15 s sans NOTIFY
                    yield f"data: {n.payload}\n\n"
                    break
                else:
                    yield ": ping\n\n"
        finally:
            await conn.close()
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/projets", response_class=HTMLResponse)
def screen_projects(request: Request, actor=Depends(current_user)):
    return page(request, "projets.html", {
        "projects": db.query(RESOURCES["projets"]["sql"]),
        "late": db.query("SELECT * FROM v_late_milestones"),
        "open": db.query("SELECT * FROM v_open_assignments"),
        "assignments": db.query(RESOURCES["affectations"]["sql"]),
        "af_fields": _options(RESOURCES["affectations"]),
    }, actor)


@app.get("/kpi", response_class=HTMLResponse)
def screen_kpi(request: Request, actor=Depends(current_user)):
    return page(request, "kpi.html", {
        "summary": db.one("SELECT * FROM v_kpi_summary"),
        "by_person": db.query("SELECT * FROM v_kpi_by_person WHERE resolved > 0 ORDER BY resolved DESC"),
        "by_category": db.query("SELECT * FROM v_kpi_by_category"),
        "monthly": db.query("SELECT * FROM v_kpi_monthly LIMIT 12"),
        "pct": db.query("SELECT pe.name, o.period, o.n, o.pct FROM v_objectives_pct o"
                        " JOIN persons pe ON pe.id = o.person_id ORDER BY o.period DESC"),
        "objectives": db.query(RESOURCES["objectifs"]["sql"]),
        "obj_fields": _options(RESOURCES["objectifs"]),
        "tickets": db.query("SELECT * FROM v_tickets LIMIT 50"),
        "glpi_url": os.environ.get("GLPI_URL", ""),
    }, actor)


@app.get("/kanban", response_class=HTMLResponse)
def screen_kanban(request: Request, actor=Depends(current_user)):
    """Suivi des affectations en 4 colonnes ; deplacement = POST /kanban/{id} (meme couche d'ecriture)."""
    return page(request, "kanban.html", {"cols": KANBAN_COLS, "cards": db.query(KANBAN_SQL)}, actor)


@app.post("/kanban/{assignment_id}")
async def kanban_move(request: Request, assignment_id: int, actor=Depends(current_user)):
    """Deplacement d'une carte. Cible : /api/kanban pour l'API, le form lui-meme pour l'UI."""
    if not same_origin(request):
        raise StarletteHTTPException(403, "origine refusee")
    try:
        guard(services.set_assignment_status, actor, "kanban", assignment_id,
              (await form_or_json(request)).get("status"))
    except StarletteHTTPException as e:
        return RedirectResponse(f"{back_to(request)}?err={quote(str(e.detail))}", status_code=303)
    return RedirectResponse(back_to(request), status_code=303)


@app.post("/glpi/sync")
def glpi_sync_form(request: Request, actor=Depends(needs("glpi.sync"))):
    """Bouton 'Synchroniser GLPI' de l'ecran KPI : meme import que l'API, retour a l'ecran."""
    if not same_origin(request):
        raise StarletteHTTPException(403, "origine refusee")
    if not os.environ.get("GLPI_URL"):
        return RedirectResponse("/kpi?err=" + quote(
            "GLPI_URL absent : renseigner GLPI_URL/GLPI_TOKEN dans l'environnement"), status_code=303)
    try:
        n = guard(glpi.sync)
    except (StarletteHTTPException, httpx.HTTPError) as e:
        return RedirectResponse("/kpi?err=" + quote("import GLPI impossible : " + str(
            getattr(e, "detail", e))), status_code=303)
    return RedirectResponse(f"/kpi?msg={n} ticket(s) synchronise(s) depuis GLPI", status_code=303)


# ------------------------------------------------------------------ CRUD generique (HTML)

@app.get("/r/{res}/new", response_class=HTMLResponse)
def form_new(request: Request, res: str, actor=Depends(current_user)):
    spec = RESOURCES.get(res) or _bad(res)
    return page(request, "form.html", {"spec": spec, "fields": _options(spec), "values": {}}, actor)


@app.post("/r/{res}/new", response_class=HTMLResponse)
async def form_create(request: Request, res: str, actor=Depends(current_user)):
    spec = RESOURCES.get(res) or _bad(res)
    if not same_origin(request):
        raise StarletteHTTPException(403, "origine refusee")
    try:
        guard(spec["create"], actor, "dashboard", await form_or_json(request))
    except StarletteHTTPException as e:
        return RedirectResponse(f"{back_to(request)}?err={quote(str(e.detail))}", status_code=303)
    return RedirectResponse(back_to(request), status_code=303)


@app.get("/r/{res}/{row_id}/edit", response_class=HTMLResponse)
def form_edit(request: Request, res: str, row_id: int, actor=Depends(current_user)):
    spec = RESOURCES.get(res) or _bad(res)
    row = db.one(f"SELECT * FROM {spec['table']} WHERE id = %s", (row_id,))
    if not row:
        raise StarletteHTTPException(404, "introuvable")
    return page(request, "form.html", {"spec": spec, "fields": _options(spec), "values": row}, actor)


@app.post("/r/{res}/{row_id}/edit", response_class=HTMLResponse)
async def form_update(request: Request, res: str, row_id: int, actor=Depends(current_user)):
    spec = RESOURCES.get(res) or _bad(res)
    if not same_origin(request):
        raise StarletteHTTPException(403, "origine refusee")
    try:
        guard(spec["update"], actor, "dashboard", row_id, await form_or_json(request))
    except StarletteHTTPException as e:
        return RedirectResponse(f"{back_to(request)}?err={quote(str(e.detail))}", status_code=303)
    return RedirectResponse(back_to(request), status_code=303)


@app.post("/r/{res}/{row_id}/delete", response_class=HTMLResponse)
async def form_delete(request: Request, res: str, row_id: int, actor=Depends(current_user)):
    spec = RESOURCES.get(res) or _bad(res)
    if not spec.get("delete"):
        raise StarletteHTTPException(400, "suppression indisponible")
    if not same_origin(request):
        raise StarletteHTTPException(403, "origine refusee")
    try:
        guard(spec["delete"], actor, "dashboard", row_id)
    except StarletteHTTPException as e:
        return RedirectResponse(f"{back_to(request)}?err={quote(str(e.detail))}", status_code=303)
    return RedirectResponse(back_to(request), status_code=303)


@app.post("/notes", response_class=HTMLResponse)
async def create_note(request: Request, actor=Depends(current_user)):
    """Note d'avancement : hors CRUD generique (table notes, pas de table de reference)."""
    if not same_origin(request):
        raise StarletteHTTPException(403, "origine refusee")
    try:
        guard(services.add_note, actor, "dashboard", await form_or_json(request))
    except StarletteHTTPException as e:
        return RedirectResponse(f"{back_to(request)}?err={quote(str(e.detail))}", status_code=303)
    return RedirectResponse(back_to(request), status_code=303)


def _bad(res):
    raise StarletteHTTPException(404, f"ressource inconnue : {res}")


def _options(spec):
    out = []
    for name, label, kind, required, opts in spec["fields"]:
        out.append((name, label, kind, required, ref_options(opts) if opts else None))
    return out


# ------------------------------------------------------------------ API JSON (meme couche d'ecriture)

@app.get("/api/kpis")
def api_kpis(actor=Depends(current_user)):
    return {
        "summary": db.one("SELECT * FROM v_kpi_summary"),
        "by_person": db.query("SELECT * FROM v_kpi_by_person"),
        "by_category": db.query("SELECT * FROM v_kpi_by_category"),
        "monthly": db.query("SELECT * FROM v_kpi_monthly LIMIT 12"),
    }


@app.get("/api/who-is-on")
def api_who_is_on(person: str | None = None, actor=Depends(current_user)):
    if person:
        return {"items": db.query("SELECT * FROM v_open_assignments WHERE lower(person) = lower(%s)", (person,))}
    return {"items": db.query("SELECT * FROM v_open_assignments")}


@app.get("/api/kanban")
def api_kanban(actor=Depends(current_user)):
    return {"cols": [{"key": k, "label": v} for k, v in KANBAN_COLS], "cards": db.query(KANBAN_SQL)}


@app.post("/api/glpi/sync")
def api_glpi_sync(actor=Depends(needs("glpi.sync"))):
    """GLPI -> kpi_raw a la demande (meme code que l'import nocturne, upsert idempotent)."""
    if not os.environ.get("GLPI_URL"):
        raise StarletteHTTPException(400, "GLPI_URL absent : renseigner GLPI_URL/GLPI_TOKEN dans l'environnement")
    return {"imported": glpi.sync(), "summary": db.one("SELECT * FROM v_kpi_summary")}


@app.get("/api/tickets")
def api_tickets(status: str | None = None, actor=Depends(current_user)):
    sql = "SELECT * FROM v_tickets"
    if status:
        sql += " WHERE status = %s"
    return {"items": db.query(sql, (status,) if status else ())}


@app.get("/api/competences/analyse")
def api_person_analysis(period: str = "all", d: str | None = None, personne: str | None = None,
                        actor=Depends(current_user)):
    """Analyse competences par PERSONNE (activite reelle : support + tickets + projets, statuts
    et SLA inclus), filtrable par periode (period=date|week|month|year, + d=AAAA-MM-JJ ancre)
    et par personne (sous-chaine du nom, insensible a la casse — comme le champ de recherche du
    dashboard)."""
    p_from, p_to = period_range(period, d)
    sql = "SELECT * FROM f_person_analysis(%s, %s)"
    params: list = [p_from, p_to]
    if personne:
        sql += " WHERE lower(name) LIKE %s"
        params.append("%" + personne.lower() + "%")
    return {"items": db.query(sql + " ORDER BY total DESC, name", tuple(params))}


@app.get("/api/persons/{person_id}/history")
def api_history(person_id: int, actor=Depends(current_user)):
    row = db.one("SELECT * FROM v_person_history WHERE person_id = %s", (person_id,))
    if not row:
        raise StarletteHTTPException(404, "personne introuvable")
    return row


@app.get("/api/audit")
def api_audit(limit: int = 50, actor=Depends(needs("audit.read"))):
    return {"items": db.query("SELECT * FROM audit_log ORDER BY at DESC LIMIT %s", (min(limit, 500),))}


@app.get("/api/{res}")
def api_list(res: str, actor=Depends(current_user)):
    spec = RESOURCES.get(res) or _bad(res)
    return {"items": db.query(spec["sql"])}


@app.post("/api/{res}", status_code=201)
async def api_create(res: str, request: Request, actor=Depends(current_user)):
    spec = RESOURCES.get(res) or _bad(res)
    return guard(spec["create"], actor, "api", await form_or_json(request))


@app.put("/api/{res}/{row_id}")
async def api_update(res: str, row_id: int, request: Request, actor=Depends(current_user)):
    spec = RESOURCES.get(res) or _bad(res)
    return guard(spec["update"], actor, "api", row_id, await form_or_json(request))


@app.delete("/api/{res}/{row_id}")
def api_delete(res: str, row_id: int, actor=Depends(current_user)):
    spec = RESOURCES.get(res) or _bad(res)
    if not spec.get("delete"):
        raise StarletteHTTPException(400, "suppression indisponible")
    return guard(spec["delete"], actor, "api", row_id)


# ------------------------------------------------------------------ MCP monte sur le meme backend

if mcp_asgi is not None:
    from .mcp_server import bearer

    app.mount("/mcp", bearer(mcp_asgi))
