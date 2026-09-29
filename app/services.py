"""Couche d'ecriture unique : dashboard, API et MCP passent tous ici (memes validations, memes droits)."""
import json
from datetime import date, datetime, timedelta

from . import db
from .auth import check_person_access, hash_password, require


def period_range(period: str, d: str | None):
    """Fenetre [debut, fin) d'une periode : jour (date AAAA-MM-JJ), semaine ISO, mois AAAA-MM,
    annee AAAA. Sans periode valide -> (None, None) = tout l'historique. Partagee par l'ecran
    web, l'API et le MCP : le filtre ne peut pas diverger d'une porte a l'autre."""
    if not d:
        now = datetime.now()
        if period == "week":
            start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            return start, start + timedelta(days=7)
        if period == "month":
            first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            nxt = (first + timedelta(days=32)).replace(day=1)
            return first, nxt
        if period == "year":
            return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0), \
                   now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0, year=now.year + 1)
        return None, None
    try:
        day = datetime.strptime(d[:10], "%Y-%m-%d")
    except ValueError:
        return None, None
    if period == "date":
        return day, day + timedelta(days=1)
    return day, day + timedelta(days=7)


def _req(data, *names):
    missing = [n for n in names if not str(data.get(n) or "").strip()]
    if missing:
        raise ValueError("champ(s) obligatoire(s) : " + ", ".join(missing))


def _named(d, *keys):
    """Complete les cles absentes avec None : psycopg exige tous les parametres nommes."""
    return {**{k: None for k in keys}, **d}


def _clean(data):
    """Recadre les valeurs : une chaine videvenue d'un formulaire vaut « champ non renseigne » (None).

    Toutes les chaines, sans exception : un '' envoye a une colonne date ou int est rejete par
    Postgres, et l Champs NOT NULL (progression) sont laisses en l'etat par _patch(skip_none=...).
    """
    return {k: (v.strip() or None) if isinstance(v, str) else v for k, v in data.items()}


def _glpi_id(data):
    """Identifiant du technicien dans l'outil de tickets : entier positif, ou vide."""
    raw = data.get("glpi_id")
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("identifiant GLPI : entier attendu")
    if value <= 0:
        raise ValueError("identifiant GLPI : entier positif attendu")
    return value


def _when(value, field):
    """Horodatage : accepte une chaine ISO ou un datetime ; refuse le vide."""
    if value in (None, ""):
        return None
    try:
        return value if hasattr(value, "tzinfo") else datetime.fromisoformat(str(value).strip().replace(" ", "T"))
    except ValueError:
        raise ValueError(f"{field} : date invalide (attendu AAAA-MM-JJ ou AAAA-MM-JJTHH:MM)")

def _category(data):
    """Cle de categorie technique : doit exister dans la table categories."""
    key = str(data.get("category") or "").strip().lower()
    if not key:
        return None
    if not db.one("SELECT 1 FROM categories WHERE lower(key) = %s", (key,)):
        raise ValueError(f"categorie inconnue : {key}")
    return key


def audit(conn, source, actor, action, entity, entity_id=None, detail=None):
    conn.execute(
        "INSERT INTO audit_log (source, actor, action, entity, entity_id, detail)"
        " VALUES (%s,%s,%s,%s,%s,%s)",
        (source, actor["email"], action, entity, str(entity_id) if entity_id is not None else None,
         json.dumps(detail or {}, default=str)),
    )


def _fetch(conn, table, row_id):
    row = conn.execute(f"SELECT * FROM {table} WHERE id = %s", (row_id,)).fetchone()
    if not row:
        raise ValueError(f"{row_id} introuvable dans {table}")
    return row


def _patch(table, row_id, data, fields, skip_none=()):
    """(sql, params) d'un UPDATE qui ne touche QUE les champs presents dans data.

    Un appel partiel (PUT de l'API, outil MCP qui ne cite qu'un champ) ne doit pas effacer les
    champs qu'il ne mentionne pas : la feuille de l'UI, elle, envoie tout et peut vider un champ.
    skip_none : champs NOT NULL qu'un formulaire vide laisse en l'etat au lieu de les mettre a NULL.
    """
    sets, params = [], {}
    for f in fields:
        if f in data and (f not in skip_none or data[f] is not None):
            sets.append(f"{f}=%({f})s")
            params[f] = data[f]
    if not sets:
        raise ValueError("aucun champ a mettre a jour")
    params["__id"] = row_id
    return f"UPDATE {table} SET {', '.join(sets)} WHERE id=%(__id)s RETURNING *", params


def _write(actor, source, action, entity, entity_id, fn, *args, **kw):
    """fn(conn) doit retourner la ligne ecrite ; l'audit part dans la meme transaction."""
    with db.connect() as conn, conn.transaction():
        row = fn(conn, *args, **kw)
        audit(conn, source, actor, action, entity, (row or {}).get("id"), row)
        return row


# -------------------------------------------------------------- personnes

def create_person(actor, source, data):
    require(actor, "persons.manage")
    d = _clean(data)
    _req(d, "name", "email", "password")
    return _write(actor, source, "create", "person", None, lambda c: c.execute(
        "INSERT INTO persons (name, email, title, role, glpi_id, password_hash) VALUES"
        " (%(name)s,%(email)s,%(title)s, COALESCE(%(role)s,'tech'), %(glpi_id)s, %(ph)s)"
        " RETURNING id, name, email, title, role, glpi_id, active",
        {**_named(d, "title", "role"), "glpi_id": _glpi_id(d), "ph": hash_password(d["password"])}
    ).fetchone())


def update_person(actor, source, person_id, data):
    check_person_access(actor, person_id, "persons.manage")
    d = _clean(data)
    if d.get("role") and d["role"] != _person_role(person_id):
        require(actor, "roles.manage")  # changer un role, c'est du ressort de qui gere les roles
    _req(d, "name", "email")
    with db.connect() as conn:
        _fetch(conn, "persons", person_id)
        old = conn.execute("SELECT password_hash FROM persons WHERE id=%s", (person_id,)).fetchone()["password_hash"]

    def fn(c):
        return c.execute(
            "UPDATE persons SET name=%(name)s, email=%(email)s, title=%(title)s,"
            " role=COALESCE(%(role)s, role), glpi_id=%(glpi_id)s,"
            " password_hash=%(ph)s WHERE id=%(id)s"
            " RETURNING id, name, email, title, role, glpi_id, active",
            {**_named(d, "title", "role"), "glpi_id": _glpi_id(d),
             "ph": hash_password(d["password"]) if d.get("password") else old,
             "id": person_id}).fetchone()

    return _write(actor, source, "update", "person", person_id, fn)


def delete_person(actor, source, person_id):
    require(actor, "persons.manage")
    return _write(actor, source, "delete", "person", person_id,
                  lambda c: c.execute("DELETE FROM persons WHERE id=%s RETURNING id", (person_id,)).fetchone())


def _person_role(person_id):
    row = db.one("SELECT role FROM persons WHERE id=%s", (person_id,))
    return row["role"] if row else None


# -------------------------------------------------------------- support

# Une intervention du support n'est ni un projet, ni un ticket de l'outil de tickets : elle est
# saisie a la main. Le technicien peut saisir la sienne ; saisir pour quelqu'un d'autre demande
# support.manage (meme regle de propriete que les competences et les affectations).
SUPPORT_STATUS = ("todo", "in_progress", "blocked", "escalade", "done")   # memes statuts qu'une fiche de projet + escalade
SUPPORT_FIELDS = "person_id, category, niveau, title, reported_at, resolved_at, status, solution"


def _support(d):
    _req(d, "person_id", "title", "reported_at")
    reported = _when(d["reported_at"], "signalement")
    resolved = _when(d.get("resolved_at"), "resolution")
    if resolved and resolved < reported:
        raise ValueError("la resolution ne peut pas preceder le signalement")
    niveau = str(d.get("niveau") or "N1").strip().upper()
    if niveau not in ("N1", "N2", "N3"):
        raise ValueError("niveau N1, N2 ou N3")
    status = str(d.get("status") or "todo").strip().lower()
    if status not in SUPPORT_STATUS:
        raise ValueError("statut A faire, En cours, Bloque, Bloque — Escalade ou Termine")
    if resolved or status == "done":
        if not resolved:                     # une fiche terminee sans resolution serait un mensonge
            raise ValueError("une fiche Terminee exige sa resolution (resolved_at)")
        status = "done"                      # fiche resolue = fiche terminee, quoi qu'on ait envoye
    return {"person_id": d["person_id"], "category": _category(d), "niveau": niveau, "title": d["title"],
            "reported_at": reported, "resolved_at": resolved, "status": status,
            "solution": d.get("solution") or None}


def create_support(actor, source, data):
    d = _clean(data)
    check_person_access(actor, d["person_id"], "support.manage")
    return _write(actor, source, "create", "support", None, lambda c: c.execute(
        f"INSERT INTO support ({SUPPORT_FIELDS}) VALUES"
        " (%(person_id)s, %(category)s, %(niveau)s, %(title)s, %(reported_at)s, %(resolved_at)s,"
        " %(status)s, %(solution)s) RETURNING id, " + SUPPORT_FIELDS, _support(d)).fetchone())


def update_support(actor, source, support_id, data):
    """Modifier une intervention exige support.manage : un enregistrement de support est un
    document d'equipe, personne ne modifie (ni ne supprime) des interventions sans ce droit.
    La saisie (create_support), elle, reste sur la regle « sa propre fiche »."""
    require(actor, "support.manage")
    d = _clean(data)
    with db.connect() as conn:
        _fetch(conn, "support", support_id)     # not-found avant permission, coût quasi nul
    return _write(actor, source, "update", "support", support_id, lambda c: c.execute(
        f"UPDATE support SET person_id=%(person_id)s, category=%(category)s, niveau=%(niveau)s,"
        f" title=%(title)s, reported_at=%(reported_at)s, resolved_at=%(resolved_at)s,"
        f" status=%(status)s, solution=%(solution)s"
        f" WHERE id=%(id)s RETURNING id, {SUPPORT_FIELDS}", {**_support(d), "id": support_id}).fetchone())


def delete_support(actor, source, support_id):
    require(actor, "support.manage")
    with db.connect() as conn:
        _fetch(conn, "support", support_id)
    return _write(actor, source, "delete", "support", support_id, lambda c: c.execute(
        "DELETE FROM support WHERE id=%s RETURNING id", (support_id,)).fetchone())


# -------------------------------------------------------------- taxonomie (onglet Categorie)

TAXO_TYPES = ("Intervention", "Administration", "Deploiement", "Autre")


def _slug(s):
    return "-".join("".join(ch if ch.isalnum() else "-" for ch in s.strip().lower()).split())


def create_category(actor, source, data):
    """Une ligne de taxonomie (Type, Classe, Niveau, SLA). Re-saisir la meme (Type, Classe, Niveau)
    met a jour son SLA : c'est ainsi que le Manager calibre la grille, en une seule operation."""
    require(actor, "data.manage")          # onglet reserve au manager (proxy : aucune nouvelle permission)
    d = _clean(data)
    typ = str(d.get("type") or "").strip()
    classe = str(d.get("classe") or "").strip()
    niv = str(d.get("niveau") or "").strip().upper()
    if typ not in TAXO_TYPES:
        raise ValueError(f"type inconnu : {typ} (Intervention, Administration, Deploiement, Autre)")
    if not classe:
        raise ValueError("classe obligatoire")
    if niv not in ("N1", "N2", "N3"):
        raise ValueError("niveau N1, N2 ou N3")
    try:
        sla = int(d["sla_minutes"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("SLA en minutes, entier > 0") from None
    if sla <= 0:
        raise ValueError("SLA en minutes, entier > 0")
    couleur = (str(d.get("couleur") or "").strip() or "#94a3b8")[:20]
    key = f"{_slug(typ)}-{_slug(classe)}-{niv.lower()}"

    def fn(c):
        return c.execute(
            "INSERT INTO categories (key, type, classe, niveau, sla_minutes, couleur)"
            " VALUES (%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (type, classe, niveau) DO UPDATE"
            " SET sla_minutes = EXCLUDED.sla_minutes, couleur = EXCLUDED.couleur"
            " RETURNING key, type, classe, niveau, sla_minutes, couleur",
            (key, typ, classe, niv, sla, couleur)).fetchone()

    row = _write(actor, source, "upsert", "category", key, fn)
    return row


def delete_category(actor, source, key):
    """Retire une ligne de la grille. Une ligne referencee par des fiches est bloquee par la FK
    (guard -> 400) : on ne perd jamais une classe encore utilisee."""
    require(actor, "data.manage")
    row = db.one("SELECT key FROM categories WHERE key = %s", (key,))
    if not row:
        raise ValueError(f"categorie inconnue : {key}")
    return _write(actor, source, "delete", "category", key, lambda c: c.execute(
        "DELETE FROM categories WHERE key=%s RETURNING key, type, classe, niveau, sla_minutes, couleur",
        (key,)).fetchone())


# -------------------------------------------------------------- competences

def upsert_skill(actor, source, data):
    d = _clean(data)
    _req(d, "person_id", "category", "level")
    d["category"] = _category(d)
    if d["category"] is None:
        raise ValueError("categorie obligatoire")
    check_person_access(actor, d["person_id"], "data.manage")
    level = int(d["level"])
    if not 1 <= level <= 5:
        raise ValueError("niveau entre 1 et 5")

    def fn(c):
        prev = c.execute("SELECT level FROM skills WHERE person_id=%s AND category=%s",
                         (d["person_id"], d["category"])).fetchone()
        c.execute(
            "INSERT INTO skills (person_id, category, level) VALUES (%(person_id)s,%(category)s,%(level)s)"
            " ON CONFLICT (person_id, category) DO UPDATE SET level=EXCLUDED.level, updated_at=now()",
            d)
        c.execute(
            "INSERT INTO skills_log (person_id, skill, level, previous_level, note, source)"
            " VALUES (%(person_id)s,%(category)s,%(level)s,%(prev)s,%(note)s,%(source)s)",
            {**_named(d, "note"), "prev": prev["level"] if prev else None, "source": source})
        return c.execute("SELECT id, person_id, category, level, updated_at FROM skills"
                         " WHERE person_id=%s AND category=%s", (d["person_id"], d["category"])).fetchone()

    return _write(actor, source, "upsert", "skill", None, fn)


# -------------------------------------------------------------- projets

def create_project(actor, source, data):
    require(actor, "projects.manage")
    d = _clean(data)
    _req(d, "name")
    return _write(actor, source, "create", "project", None, lambda c: c.execute(
        "INSERT INTO projects (name, status, start_date, due_date, description, category, done_at)"
        " VALUES (%(name)s, COALESCE(%(status)s,'planned'), %(start_date)s, %(due_date)s, %(description)s,"
        " %(category)s, %(done_at)s) RETURNING *",
        {**_named(d, "status", "start_date", "due_date", "description", "done_at"),
         "category": _category(d)}).fetchone())


def update_project(actor, source, project_id, data):
    require(actor, "projects.manage")
    d = _clean(data)
    _req(d, "name")
    if "category" in d:
        d["category"] = _category(d)
    if d.get("status") == "done" and "done_at" not in d:
        # un projet passe en « termine » sans date de fin : on met la jour du jour, sinon
        # l'analyse « realise dans les delais » n'a aucun repere.
        d["done_at"] = date.today()
    if "done_at" in d:
        d["done_at"] = _when(d["done_at"], "fin")
    with db.connect() as conn:
        _fetch(conn, "projects", project_id)
    return _write(actor, source, "update", "project", project_id,
                  lambda c: c.execute(*_patch("projects", project_id, d,
                                              ("name", "status", "start_date", "due_date", "description",
                                               "category", "done_at"))).fetchone())


def delete_project(actor, source, project_id):
    require(actor, "projects.manage")
    return _write(actor, source, "delete", "project", project_id,
                  lambda c: c.execute("DELETE FROM projects WHERE id=%s RETURNING id", (project_id,)).fetchone())


# -------------------------------------------------------------- affectations

def create_assignment(actor, source, data):
    require(actor, "assignments.manage")
    d = _clean(data)
    _req(d, "project_id", "person_id", "role")
    return _write(actor, source, "create", "assignment", None, lambda c: c.execute(
        "INSERT INTO assignments (project_id, person_id, role, status, start_date, due_date, progress)"
        " VALUES (%(project_id)s,%(person_id)s,%(role)s, COALESCE(%(status)s,'todo'), %(start_date)s,"
        " %(due_date)s, COALESCE(%(progress)s,0)) RETURNING *",
        _named(d, "status", "start_date", "due_date", "progress")).fetchone())


def update_assignment(actor, source, assignment_id, data):
    d = _clean(data)
    _req(d, "role")
    with db.connect() as conn:
        row = _fetch(conn, "assignments", assignment_id)
    check_person_access(actor, row["person_id"], "assignments.manage")  # le technicien fait avancer sa tache
    return _write(actor, source, "update", "assignment", assignment_id, lambda c: c.execute(*_patch(
        "assignments", assignment_id, d,
        ("role", "status", "start_date", "due_date", "progress", "project_id", "person_id"),
        skip_none=("progress",))).fetchone())   # progression NOT NULL : un champ vide ne l'ecrase pas


def set_assignment_status(actor, source, assignment_id, status, progress=None):
    """Deplacement d'une carte dans le Kanban : memes droits que l'edition d'affectation."""
    if status not in ("todo", "in_progress", "done", "blocked"):
        raise ValueError(f"statut inconnu : {status}")
    with db.connect() as conn:
        row = _fetch(conn, "assignments", assignment_id)
    check_person_access(actor, row["person_id"], "assignments.manage")
    return _write(actor, source, "kanban", "assignment", assignment_id, lambda c: c.execute(
        "UPDATE assignments SET status=%s, progress=COALESCE(%s, progress) WHERE id=%s RETURNING *",
        (status, progress, assignment_id)).fetchone())


def delete_assignment(actor, source, assignment_id):
    require(actor, "assignments.manage")
    return _write(actor, source, "delete", "assignment", assignment_id,
                  lambda c: c.execute("DELETE FROM assignments WHERE id=%s RETURNING id", (assignment_id,)).fetchone())


def add_note(actor, source, data):
    """Note d'avancement horodatee ; met aussi a jour l'avancement de l'affectation."""
    d = _clean(data)
    _req(d, "assignment_id", "text")
    with db.connect() as conn:
        row = _fetch(conn, "assignments", d["assignment_id"])
    check_person_access(actor, row["person_id"], "data.manage")

    def fn(c):
        note = c.execute(
            "INSERT INTO notes (assignment_id, author_id, progress, text) VALUES (%(assignment_id)s,%(author_id)s,"
            " %(progress)s, %(text)s) RETURNING *",
            {**_named(d, "progress"), "author_id": actor["id"]}).fetchone()
        if d.get("progress") not in (None, ""):
            c.execute("UPDATE assignments SET progress=%s WHERE id=%s", (d["progress"], d["assignment_id"]))
        return note

    return _write(actor, source, "note", "note", None, fn)


# -------------------------------------------------------------- objectifs

def set_objective(actor, source, data):
    d = _clean(data)
    _req(d, "person_id", "period", "title")
    check_person_access(actor, d["person_id"], "data.manage")

    def fn(c):
        return c.execute(
            "INSERT INTO objectives (person_id, period, title, target, achieved)"
            " VALUES (%(person_id)s,%(period)s,%(title)s, %(target)s, %(achieved)s)"
            " ON CONFLICT (person_id, period, title) DO UPDATE SET"
            " target=EXCLUDED.target, achieved=EXCLUDED.achieved"
            " RETURNING *", _named(d, "target", "achieved")).fetchone()

    return _write(actor, source, "upsert", "objective", None, fn)


def delete_objective(actor, source, objective_id):
    with db.connect() as conn:
        row = _fetch(conn, "objectives", objective_id)
    check_person_access(actor, row["person_id"], "data.manage")
    return _write(actor, source, "delete", "objective", objective_id,
                  lambda c: c.execute("DELETE FROM objectives WHERE id=%s RETURNING id", (objective_id,)).fetchone())


# -------------------------------------------------------------- roles et permissions

def create_role(actor, source, data):
    require(actor, "roles.manage")
    d = _clean(data)
    _req(d, "name")
    name = d["name"].lower()
    if not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError("nom de role : minuscules, chiffres, - et _ seulement")
    return _write(actor, source, "create", "role", None, lambda c: c.execute(
        "INSERT INTO roles (name, description) VALUES (%(name)s, %(description)s)"
        " RETURNING name, description", {"name": name, "description": d.get("description") or ""}).fetchone())


def set_role_permissions(actor, source, role, perms):
    """Remplace toutes les permissions d'un role par la liste recue (cases a cocher)."""
    require(actor, "roles.manage")
    wanted = sorted({str(p).strip() for p in (perms or []) if str(p).strip()})
    known = {r["key"] for r in db.query("SELECT key FROM permissions")}
    unknown = [p for p in wanted if p not in known]
    if unknown:
        raise ValueError("permission(s) inconnue(s) : " + ", ".join(unknown))

    def fn(c):
        if not c.execute("SELECT 1 FROM roles WHERE name = %s FOR UPDATE", (role,)).fetchone():
            raise ValueError(f"role inconnu : {role}")
        c.execute("DELETE FROM role_permissions WHERE role = %s", (role,))
        for p in wanted:
            c.execute("INSERT INTO role_permissions (role, perm) VALUES (%s, %s)", (role, p))
        return c.execute("SELECT name, description FROM roles WHERE name = %s", (role,)).fetchone()

    return _write(actor, source, "update", "role", role, fn)


def delete_role(actor, source, role):
    require(actor, "roles.manage")
    used = db.one("SELECT count(*) n FROM persons WHERE role = %s", (role,))["n"]
    if used:
        raise ValueError(f"{used} personne(s) porte(nt) encore ce role : reassignez-les d'abord")
    return _write(actor, source, "delete", "role", role,
                  lambda c: c.execute("DELETE FROM roles WHERE name = %s RETURNING name", (role,)).fetchone())

