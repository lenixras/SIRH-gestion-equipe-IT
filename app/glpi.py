"""Client GLPI et import des KPI tickets.

API REST v1 (apirest.php) : App-Token + `Authorization: user_token ...` pour `initSession`, puis
Session-Token sur tous les appels suivants (GLPI 11 renvoie 400 ERROR_SESSION_TOKEN_MISSING
sinon). Une seule recherche paginee suffit : `forcedisplay[]` selectionne les colonnes
(listSearchOptions Ticket de GLPI 11), dont le technicien et les chronometres. Les chronometres
GLPI sont des delais en secondes (takeintoaccount_delay_stat, solve_delay_stat) : on reconstruit
les horodatages, jamais de calcul LLM.

Config : GLPI_URL, GLPI_TOKEN (app-token), GLPI_PASSWORD (user-token), GLPI_SINCE (AAAA-MM-JJ),
GLPI_API_PATH (defaut /glpi/apirest.php pour le simulateur, /apirest.php sur une instance GLPI).
"""
import datetime as dt
import os
import uuid

from . import db
from .services import _slug

# statuts ticket GLPI (codes en dur cote GLPI, non exposes par l'API) ; 10 = approbation (GLPI 11)
STATUS = {1: "nouveau", 2: "encours", 3: "encours", 4: "attente", 5: "resolu", 6: "resolu", 10: "approbation"}

# colonnes de la recherche Ticket (id listSearchOptions GLPI 11) -> champ lu par normalize()
SEARCH_COLUMNS = {"1": "name", "2": "id", "3": "priority", "5": "users_id_tech", "7": "itilcategories_name",
                  "12": "status", "14": "type", "15": "date_creation", "16": "closedate", "17": "solvedate",
                  "150": "takeintoaccount_delay_stat", "154": "solve_delay_stat"}


def _ts(value):
    """GLPI renvoie une heure locale sans fuseau : on la lit en UTC. MTTA/MTTR sont des differences,
    donc un decalage uniforme ne change pas les KPI (seulement l'heure affichee)."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, dt.timezone.utc)
    out = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return out if out.tzinfo else out.replace(tzinfo=dt.timezone.utc)


def _first(ticket, *names, default=None):
    for n in names:
        if ticket.get(n) not in (None, "", 0):
            return ticket[n]
    return default


def _person_id(conn, name, glpi_id=None):
    """Rapprochement GLPI -> personne : l'identifiant du technicien gagne, le nom reste le repli.

    Le nom seul confond deux homonymes (le premier de la table raflait tous les tickets) ; avec
    persons.glpi_id, le lien est explicite. Sans identifiant saisi, le nom continue de matcher.
    """
    if glpi_id not in (None, "", 0):
        row = conn.execute("SELECT id FROM persons WHERE glpi_id = %s", (int(glpi_id),)).fetchone()
        if row:
            return row["id"]
    if not name:
        return None
    row = conn.execute("SELECT id FROM persons WHERE lower(name) = lower(%s)", (str(name),)).fetchone()
    return row["id"] if row else None


def _rows(body):
    """`data` revient en objet indexe par id (doc GLPI) ou en liste (GLPI 11) selon la version ;
    une reponse entierement listee (GET /ITILCategory) est la liste elle-meme."""
    if isinstance(body, list):
        return body
    data = body.get("data") or body.get("items") or []
    return list(data.values()) if isinstance(data, dict) else list(data)


def fetch(url=None, token=None, user_token=None, since=None):
    """Liste les tickets GLPI (JSON bruts). Une session par appel, fermee automatiquement."""
    import httpx

    base = (url or os.environ["GLPI_URL"]).rstrip("/")
    headers = {"App-Token": token or os.environ["GLPI_TOKEN"], "Content-Type": "application/json",
               "Authorization": "user_token " + (user_token or os.environ.get("GLPI_PASSWORD", ""))}
    api = os.environ.get("GLPI_API_PATH", "/glpi/apirest.php").rstrip("/")
    # ponytail: 500 tickets et 1000 utilisateurs par page ; affiner range/filtres au-dela.
    page = 500
    with httpx.Client(base_url=base + api, headers=headers, timeout=30) as s:
        s.headers["Session-Token"] = s.get("/initSession").json()["session_token"]
        rows, lo = [], 0
        while True:
            body = s.get("/search/Ticket", params={
                "range": f"{lo}-{lo + page - 1}", "forcedisplay[]": list(SEARCH_COLUMNS),
                **({"filter[15]": ">" + (since or os.environ["GLPI_SINCE"])}
                   if since or os.environ.get("GLPI_SINCE") else {})}).json()
            page_rows = _rows(body)
            rows += page_rows
            lo += page
            if not page_rows or lo >= int(body.get("totalcount") or 0):
                break
        # la colonne technicien est un id utilisateur : une seule recherche User fait la correspondance
        users = {str(u.get("2")): u.get("1") for u in _rows(s.get(
            "/search/User", params={"range": "0-999", "forcedisplay[]": ["1", "2"]}).json())}
        for r in rows:
            r.update({v: r[k] for k, v in SEARCH_COLUMNS.items() if k in r})
            r["users_name"] = users.get(str(r.get("users_id_tech")), "")
        return rows


def normalize(ticket, conn):
    """Ticket GLPI ou JSON normalise -> ligne kpi_raw (horodatages reconstruits).

    Un ticket jamais pris en charge n'a pas de MTTA, un ticket non resolu pas de MTTR : sans cette
    regle, le premier import comptait tout le portefeuille comme resolu (MTTR = 0).
    """
    opened = _ts(_first(ticket, "opened_at", "date_creation"))
    if not opened:
        return None
    status = STATUS.get(int(_first(ticket, "status", default=1) or 1), "nouveau")
    tto = float(_first(ticket, "takeintoaccount_delay_stat", "time_to_take_into_account", default=0) or 0)
    ttr = float(_first(ticket, "solutiontime", "solve_delay_stat", "time_to_resolve", default=0) or 0)
    taken = _ts(_first(ticket, "taken_at")) or (opened + dt.timedelta(seconds=tto) if tto or status != "nouveau" else None)
    solved = _ts(_first(ticket, "solved_at", "solvedate", "closedate")) or (
        taken + dt.timedelta(seconds=ttr) if taken and (ttr or status == "resolu") else None)
    priority = _first(ticket, "priority")
    # la categorie GLPI est le chemin complet (« Incident > Reseau ») : seule sa feuille est une
    # classe de la taxonomie (qui porte Niveau + SLA), d'ou l'epluchage ici meme.
    category = str(_first(ticket, "category", "itilcategories_name",
                   default="Demande" if str(ticket.get("type")) == "2" else "Incident"))
    return (
        str(_first(ticket, "id", "external_id", default=uuid.uuid4())),
        _first(ticket, "name", "title"),
        status,
        category.split(" > ")[-1].strip(),
        None if priority in (None, "") else int(priority),   # la recherche GLPI renvoie du texte
        _person_id(conn, _first(ticket, "person", "person_name", "users_name", "assignee"),
                   _first(ticket, "users_id_tech", "technician_id")),
        opened, taken, solved,
    )


def import_tickets(tickets, source="glpi"):
    """Upsert idempotent sur (source, external_id) : le meme import peut tourner la nuit."""
    n = 0
    with db.connect() as c:
        for t in tickets:
            row = normalize(t, c)
            if row is None:
                continue
            c.execute(
                "INSERT INTO kpi_raw (source, external_id, title, status, category, priority, person_id,"
                " opened_at, taken_at, solved_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (source, external_id) DO UPDATE SET title=EXCLUDED.title, status=EXCLUDED.status,"
                " category=EXCLUDED.category, priority=EXCLUDED.priority, person_id=EXCLUDED.person_id,"
                " opened_at=EXCLUDED.opened_at, taken_at=EXCLUDED.taken_at, solved_at=EXCLUDED.solved_at,"
                " imported_at=now()",
                (source, *row))
            n += 1
    return n


def fetch_categories(url=None, token=None, user_token=None):
    """Liste les CATEGORIES GLPI (JSON bruts) : elles rejoignent la taxonomie (classes) pour que
    la chaine SLA / Niveau / Couleur cible celles du ticket. GLPI 11 n'expose PAS /Category (le
    simulateur, si) : on tente d'abord le chemin moderne `/ITILCategory` (liste d'objets name +
    completename), puis `/Category` en repli. Sans categorie recuperee, un ticket ne rejoint aucune
    classe et son SLA reste vide (le badge « — ») — c'est ce chemin qui le remplit."""
    import httpx

    base = (url or os.environ["GLPI_URL"]).rstrip("/")
    headers = {"App-Token": token or os.environ["GLPI_TOKEN"], "Content-Type": "application/json",
               "Authorization": "user_token " + (user_token or os.environ.get("GLPI_PASSWORD", ""))}
    api = os.environ.get("GLPI_API_PATH", "/glpi/apirest.php").rstrip("/")
    with httpx.Client(base_url=base + api, headers=headers, timeout=30) as s:
        s.headers["Session-Token"] = s.get("/initSession").json()["session_token"]
        out = []
        for path, as_search in (("/ITILCategory", False), ("/search/ITILCategory", True), ("/Category", False)):
            r = s.get(path, params={"range": "0-999", "forcedisplay[]": ["1", "2", "14", "9"]}
                      if as_search else {"range": "0-999"})
            if r.status_code != 200:
                continue
            rows = _rows(r.json())
            for x in rows:
                if as_search and isinstance(x, dict) and set(x) <= {"1", "2", "3", "4", "9", "14"}:
                    x = {"name": x.get("14") or x.get("1"), "completename": x.get("1"),   # 1 = nom complet
                         "color": x.get("9") or None}
                if isinstance(x, dict) and (x.get("name") or x.get("completename") or x.get("1")):
                    out.append(x)
            if out:
                break
        return out


def import_categories(categories, source="glpi"):
    """Categories GLPI -> classes de la taxonomie (type Intervention) : une classe inconnue recoit
    ses 3 niveaux avec un SLA par defaut (30/60/120) et la couleur GLPI ; une classe deja calibree
    garde son SLA et recoit (ou reprend) la couleur. Une couleur absente ne touche pas une couleur
    deja saisie / importee. Le RAPPROCHEMENT est normalise (cat_norm : « Reseau » = « Réseau »),
    donc support et ticketing partagent bien la MEME base de connaissance au lieu de creer un
    doublon accentue. Idempotent, rappelable a chaque synchronisation."""
    n = 0
    with db.connect() as c:
        for cat in categories:
            leaf = str(_first(cat, "name", "completename") or "").strip().split(" > ")[-1].strip()
            if not leaf:
                continue
            couleur = _first(cat, "color") or None
            # la classe existe deja sous une autre orthographe (accents) : on la rejoint, on ne la
            # duplique pas — c'est la cle de voute du SLA commun support / ticketing.
            existing = c.execute(
                "SELECT classe FROM categories WHERE cat_norm(classe) = cat_norm(%s)"
                " ORDER BY niveau LIMIT 1", (leaf,)).fetchone()
            if existing:
                if couleur:
                    c.execute("UPDATE categories SET couleur = %s WHERE cat_norm(classe) = cat_norm(%s)",
                              (couleur, existing["classe"]))
                n += 1
                continue
            root = _slug(leaf)
            for niv, sla in (("N1", 30), ("N2", 60), ("N3", 120)):
                c.execute(
                    "INSERT INTO categories (key, type, classe, niveau, sla_minutes, couleur)"
                    " VALUES (%s,%s,%s,%s,%s,%s)"
                    " ON CONFLICT (type, classe, niveau) DO UPDATE"
                    " SET couleur = COALESCE(EXCLUDED.couleur, categories.couleur)",
                    (f"{root}-{niv.lower()}" if niv != "N1" else root,
                     "Intervention", leaf, niv, sla, couleur))
                n += 1
    return n


def sync(url=None, token=None, user_token=None, since=None):
    """GLPI -> kpi_raw + taxonomie. Les categories GLPI deviennent des classes (SLA/NIVEAU/COULEUR) ;
    une instance sans endpoint /Category (comme le simulateur) n'empeche pas l'import des tickets.
    Retourne le nombre de tickets : c'est ce qu'affiche l'ecran KPI."""
    db.init()
    # ponytail: les categories font l'objet d'un appel API separe ; en l'absence d'endpoint on les
    # ignore sans faire echouer l'import des tickets (premier besoin).
    try:
        import_categories(fetch_categories(url, token, user_token))
    except Exception:
        pass
    return import_tickets(fetch(url, token, user_token, since))
