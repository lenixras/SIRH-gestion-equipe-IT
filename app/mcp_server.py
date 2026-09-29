"""Serveur MCP (FastMCP) : lectures sur des vues curatees, ecritures gatees via app.services.

Aucun outil SQL libre : les lectures passent par une liste de vues, avec statement_timeout et
plafond de lignes ; les ecritures reutilisent la couche FastAPI (validations + droits + audit).
"""
import json
import os
import secrets
from datetime import datetime, timedelta

from fastmcp import FastMCP
from starlette.responses import PlainTextResponse

from . import auth, db, services

MAX_ROWS = int(os.environ.get("MCP_MAX_ROWS", "200"))
TIMEOUT_MS = int(os.environ.get("MCP_QUERY_TIMEOUT_MS", "5000"))

mcp = FastMCP("sirh-equipe", instructions=(
    "Pilotage d'equipe technique : qui fait quoi, competences, jalons, interventions support, KPI incidents. "
    "Les outils renvoient du JSON texte. Les ecritures exigent MCP_WRITE=1 cote serveur.\n"
    "Trois familles : (1) projets et affectations, (2) support = interventions techniques saisies a la main "
    "(log_support / list_support), qui ne sont ni des projets ni des tickets de l'outil de tickets, "
    "(3) competences : get_person_analysis = l'analyse PAR PERSONNE du dashboard (activite reelle : "
    "support, tickets, projets) ; set_skill / get_skills = grille manuelle de niveaux 1-5 declarés "
    "par le manager, sans ecran dedie. Le NIVEAU (N1..N3) est un attribut des TACHES, jamais des "
    "personnes. Toute tache se range dans l'arborescence Type -> Classe -> Niveau -> SLA (onglet Categorie, "
    "manager) : une fiche close au MTTR superieur a son SLA est hors delai (rouge), sinon tenu (vert)."
))


def _json(rows):
    # JSON texte : dates/Decimal de Postgres ne passent pas dans un schema de sortie structure.
    return json.dumps(rows, default=str, ensure_ascii=False, indent=1)[:20000]


def _read(view, where="TRUE", params=(), order="", limit=None):
    sql = f"SELECT * FROM {view} WHERE {where}"
    if order:
        sql += f" ORDER BY {order}"
    sql += f" LIMIT {min(limit or MAX_ROWS, MAX_ROWS)}"
    return _json(db.query(sql, params, timeout_ms=TIMEOUT_MS))


def _write(fn, *args, **kw):
    if os.environ.get("MCP_WRITE") != "1":
        raise ValueError("ecritures MCP desactivees : poser MCP_WRITE=1 sur le serveur")
    return json.dumps(fn(auth.mcp_actor(), "mcp", *args, **kw), default=str, ensure_ascii=False)


def _resolve(kind, ref):
    """Accepte un id ou un nom : « la tache X a Priya » depuis un chat."""
    row = db.one(f"SELECT * FROM {kind} WHERE id = %s", (ref,)) if str(ref).isdigit() else \
        db.one(f"SELECT * FROM {kind} WHERE lower(name) = lower(%s)", (str(ref),))
    if not row:
        raise ValueError(f"{kind} introuvable : {ref}")
    return row["id"]


# ------------------------------------------------------------------ lectures

@mcp.tool
def who_is_on(person: str | None = None) -> str:
    """Affectations en cours. Sans argument, toute l'equipe ; sinon une personne (nom ou id)."""
    if person:
        return _read("v_open_assignments", "lower(person) = lower(%s)", (person,))
    return _read("v_open_assignments")


@mcp.tool
def list_support(person: str | None = None, category: str | None = None) -> str:
    """Interventions support saisies a la main (panne reseau, serveur, infrastructure).

    Sans argument, toutes les interventions. Filtrable par technicien ou par categorie (cle ou
    classe de l'arborescence : helpdesk, reseau, serveur, base-donnees, infrastructure...).
    Colonnes : signale, resolu, type, classe, niveau, SLA (ok/depasse), MTTD, MTTR, solution.
    """
    where, params = [], []
    if person:
        where.append("lower(person) = lower(%s)")
        params.append(person)
    if category:
        where.append("(lower(category) = lower(%s) OR lower(classe) = lower(%s))")
        params.extend([category, category])
    return _read("v_support", " AND ".join(where) or "TRUE", params, order="reported_at DESC")


@mcp.tool
def get_person_analysis(person: str | None = None, period: str = "all", since: str | None = None,
                        until: str | None = None) -> str:
    """Analyse des competences PAR PERSONNE, lue sur ce qui est reellement traite (interventions
    support + tickets GLPI + projets), meme requete que le dashboard : reussite (statuts inclus),
    % de SLA tenus (support + tickets), difficulte des taches (diff_moy / diff_max = niveau N1..N3
    le plus haut tenu dans le delai), escalades (fiches « Bloque — Escalade ») et non soldes,
    realisation des projets. Le niveau est un attribut des taches, pas des personnes.

    Filtres comme l'ecran : person = sous-chaine du nom (insensible a la casse, « pri » trouve
    Priya) ; period = week|month|year (fenetre courante, meme calcul que le web), ou since / until
    ('AAAA-MM-JJ', until inclus) pour une fenetre explicite. Omettez tout pour toute l'equipe sur
    tout l'historique.
    """
    if period != "all" or since or until:
        p_from, p_to = services.period_range(period, None)
        if since:
            p_from = datetime.strptime(since[:10], "%Y-%m-%d")
        if until:
            p_to = datetime.strptime(until[:10], "%Y-%m-%d") + timedelta(days=1)
        sql = "SELECT * FROM f_person_analysis(%s, %s)"
        params: list = [p_from, p_to]
        if person:
            sql += " WHERE lower(name) LIKE %s"
            params.append("%" + person.lower() + "%")
        return _json(db.query(sql + " ORDER BY total DESC, name", tuple(params)))
    if person:
        return _read("v_person_analysis", "lower(name) LIKE %s", ("%" + person.lower() + "%",))
    return _read("v_person_analysis", order="total DESC")


@mcp.tool
def list_persons() -> str:
    """Liste de l'equipe (id, nom, poste, role, actif)."""
    return _json(db.query("SELECT id, name, title, role, active FROM persons ORDER BY active DESC, name"))


@mcp.tool
def list_projects() -> str:
    """Projets avec echeance, plus les jalons en retard."""
    return _json({"projects": db.query("SELECT * FROM projects ORDER BY due_date NULLS LAST"), "late": db.query("SELECT * FROM v_late_milestones")})


@mcp.tool
def get_project(project: str) -> str:
    """Detail d'un projet (nom ou id) : affectations ouvertes, avancement, retard."""
    pid = _resolve("projects", project)
    return _json({
        "project": db.one("SELECT * FROM projects WHERE id = %s", (pid,)),
        "assignments": db.query("SELECT * FROM v_open_assignments WHERE project_id = %s", (pid,)),
        "milestones_late": db.query("SELECT * FROM v_late_milestones WHERE id = %s", (pid,)),
    })


@mcp.tool
def get_person_history(person: str) -> str:
    """Fiche d'une personne : affectations, historique date des competences, dernieres notes."""
    return _json(db.one("SELECT * FROM v_person_history WHERE person_id = %s", (_resolve("persons", person),)))


@mcp.tool
def get_skills(person: str | None = None) -> str:
    """Grille MANUELLE de niveaux 1-5 declarés par le manager (table skills), par personne et par
    base de connaissance. Ce n'est PAS le dashboard : l'ecran /competences affiche l'analyse par
    activite reelle (get_person_analysis) ; la grille n'a plus d'ecran dedie, elle se lit ici ou
    via GET /api/competences. Niveau max si plusieurs entrees de la meme classe."""
    if person:
        return _read("v_skill_matrix", "lower(name) = lower(%s)", (person,))
    return _read("v_skill_matrix", order="name")


@mcp.tool
def get_kpis(month: str | None = None) -> str:
    """KPI incidents : MTTA/MTTR global, par personne, par categorie, par mois (AAAA-MM)."""
    out = {
        "summary": db.one("SELECT * FROM v_kpi_summary"),
        "by_person": db.query("SELECT * FROM v_kpi_by_person WHERE resolved > 0 ORDER BY resolved DESC"),
        "by_category": db.query("SELECT * FROM v_kpi_by_category"),
        "monthly": db.query("SELECT * FROM v_kpi_monthly LIMIT 12"),
    }
    if month:
        out["month"] = db.query("SELECT * FROM v_kpi_monthly WHERE to_char(month,'YYYY-MM') = %s", (month,))
    return _json(out)


@mcp.tool
def get_objectives(person: str | None = None) -> str:
    """Objectifs individuels et % d'atteinte par periode."""
    if person:
        return _read("v_objectives_pct", "person_id = %s", (_resolve("persons", person),))
    return _read("v_objectives_pct")


# ------------------------------------------------------------------ ecritures (gatees)

@mcp.tool
def assign_task(project: str, person: str, role: str, due_date: str | None = None,
                start_date: str | None = None) -> str:
    """Affecte une tache a quelqu'un (projet et personne par nom ou id). Ecriture gatee + audit."""
    return _write(services.create_assignment, {
        "project_id": _resolve("projects", project), "person_id": _resolve("persons", person),
        "role": role, "due_date": due_date or None, "start_date": start_date or None, "status": "todo"})


@mcp.tool
def log_progress(assignment_id: int, text: str, progress: int | None = None) -> str:
    """Note d'avancement horodatee ; met a jour l'avancement de l'affectation."""
    return _write(services.add_note, {"assignment_id": assignment_id, "text": text,
                                      "progress": progress})


@mcp.tool
def update_assignment(assignment_id: int, status: str | None = None, progress: int | None = None,
                      due_date: str | None = None) -> str:
    """Change le statut / l'avancement / l'echeance d'une affectation.

    Seuls les arguments fournis sont modifies : leave status a None pour y toucher.
    """
    row = db.one("SELECT role FROM assignments WHERE id = %s", (assignment_id,))
    if not row:
        raise ValueError(f"affectation introuvable : {assignment_id}")
    data = {"role": row["role"]}   # requis par la couche d'ecriture, jamais change ici
    for field, value in (("status", status), ("progress", progress), ("due_date", due_date)):
        if value is not None:
            data[field] = value
    return _write(services.update_assignment, assignment_id, data)


@mcp.tool
def log_support(person: str, category: str, title: str, reported_at: str,
                resolved_at: str | None = None, solution: str | None = None,
                niveau: str = "N1", status: str = "todo") -> str:
    """Enregistre une intervention technique (ni projet, ni ticket de l'outil de tickets).

    Technicien par nom ou id. Categorie : une cle de l'arborescence (helpdesk, reseau, serveur,
    base-donnees, infrastructure, securite...) ou un nom de classe (ex. 'Reseau'). Niveau : N1, N2
    ou N3 (N1 par defaut) ; le SLA de la fiche se lit sur (classe, niveau).
    Horodatages 'AAAA-MM-JJTHH:MM' (ou 'AAAA-MM-JJ' pour une journee entiere).
    reported_at = signalement (depart du MTTD), resolved_at = resolution (MTTR) ; vide = en cours.
    status : A faire (todo) par defaut, En cours, Bloque, Bloque — Escalade (escalade : difficulte
    necessitant une escalade) ou Termine — une fiche resolue est forcement
    Termine, une fiche Termine sans resolution est refusee.
    Pour sa propre intervention, n'importe quel technicien suffit ; pour celle d'un autre, le role
    doit porter support.manage. Ecriture gatee + audit.
    """
    return _write(services.create_support, {
        "person_id": _resolve("persons", person), "category": category, "niveau": niveau,
        "title": title, "reported_at": reported_at, "resolved_at": resolved_at,
        "solution": solution, "status": status})


@mcp.tool
def set_skill(person: str, category: str, level: int, note: str | None = None) -> str:
    """Fixe le niveau MANUEL d'une personne (1-5) dans une categorie technique et journalise
    l'evolution datee. Alimente la grille (get_skills) ; le dashboard competence ne s'en sert pas.

    Categorie : une cle de l'arborescence (helpdesk, reseau, serveur, base-donnees...). Une
    competence est definie par (personne, categorie) : la re-saisir met a jour le niveau.
    """
    return _write(services.upsert_skill, {"person_id": _resolve("persons", person), "category": category,
                                          "level": level, "note": note})


@mcp.tool
def set_objective(person: str, period: str, title: str, target: float | None = None,
                  achieved: float | None = None) -> str:
    """Cree ou met a jour un objectif individuel (periode = '2026-Q1')."""
    return _write(services.set_objective, {"person_id": _resolve("persons", person), "period": period,
                                           "title": title, "target": target, "achieved": achieved})


# ------------------------------------------------------------------ transport

def bearer(asgi):
    """Garde-fou streamable-HTTP : jeton statique partage si MCP_TOKEN est pose (aucun si absent)."""
    token = os.environ.get("MCP_TOKEN", "")

    async def app(scope, receive, send):
        if scope["type"] == "http" and token:
            header = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
            if not secrets.compare_digest(header, f"Bearer {token}"):
                return await PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
        return await asgi(scope, receive, send)

    return app
