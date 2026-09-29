"""GLPI simplifie : l'API REST v1 de GLPI (apirest.php), sans PHP ni MariaDB.

But : disposer d'un helpdesk de simulation pour exercer l'import KPI
(scripts/manage.py import-kpi --glpi) et l'etat des tickets, sans deployer GLPI.
Endpoints implemmentes : initSession, killSession, search/Ticket, Ticket/{id} (GET et POST),
getGlpiConfig, getMyProfiles. Auth : App-Token + (user_token | Session-Token), comme GLPI.

    docker build -t sirh-glpi-sim mock-glpi
    docker run --rm -p 8081:8081 sirh-glpi-sim
    GLPI_URL=http://localhost:8081 GLPI_TOKEN=simtoken GLPI_PASSWORD=simuser \\
      python3 -m scripts.manage import-kpi --glpi

POST /glpi/apirest.php/Ticket/{id} simule le travail du helpdesk (affectation a un technicien,
changement de statut) : les chronometres sont recalcules, l'import suivant voit la nouvelle etat.
"""
import html
import json
import os
import random
import re
import secrets
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

APP_TOKEN = os.environ.get("MOCK_GLPI_APP_TOKEN", "simtoken")
USER_TOKEN = os.environ.get("MOCK_GLPI_USER_TOKEN", "simuser")
PORT = int(os.environ.get("MOCK_GLPI_PORT", "8081"))
# MOCK_GLPI_STRICT=1 : exige un Session-Token (comme une instance GLPI configuree en lecture seule)
STRICT = os.environ.get("MOCK_GLPI_STRICT", "") == "1"
# noms volontairement egaux a ceux du SIRH : l'import rapproche par nom (app/glpi.py)
TECHS = os.environ.get("MOCK_GLPI_TECHS", "Marc,Priya,Ana,Luis").split(",")
# Les tickets du helpdesk portent les MEMES bases de connaissance que le support (classes de
# l'arborescence Categorie), orthographie GLPI (accents) : c'est cat_norm qui les rapproche de la
# grille, donc chaque ticket recoit le SLA (classe, niveau) partage avec l'onglet Support.
CATEGORIES = {1: "Helpdesk / poste de travail", 2: "Réseau", 3: "Sécurité",
              4: "Serveur / stockage", 5: "Base de données", 6: "Infrastructure", 7: "Application"}
CATEGORY_COLORS = {1: "#7c8ca3", 2: "#2f5d8a", 3: "#a12b2b", 4: "#1c6b3d",
                   5: "#6b5b95", 6: "#8a5b2f", 7: "#2f8a8a"}
# statuts GLPI (codes en dur dans GLPI) : 1 nouveau 2/3 en cours 4 attente 5 resolu 6 cloture
NEW, PROCESSING, PLANNED, PENDING, SOLVED, CLOSED = 1, 2, 3, 4, 5, 6

SEED = [
    # (titre, categorie, type, priorite, statut, technicien, age en heures)
    # categories = classes partagees avec le support (arborescence Categorie), pas un vocabulaire parallele
    ("Partage de fichier impossible", 2, 1, 4, SOLVED, "Priya", 96),
    ("Compte bloque apres 3 essais", 1, 1, 3, SOLVED, "Marc", 120),
    ("Demande de poste de travail", 1, 2, 2, SOLVED, "Ana", 200),
    ("Disque plein sur le serveur de fichiers", 4, 1, 5, CLOSED, "Luis", 240),
    ("Ecran noir sur le portable", 1, 1, 3, SOLVED, "Priya", 72),
    ("Installation de VPN pour un nouveau", 2, 2, 3, SOLVED, "Ana", 48),
    ("Imprimante hors ligne (bureau 2e)", 1, 1, 2, SOLVED, "Marc", 24),
    ("Sauvegarde du NAS en echec", 4, 1, 5, PROCESSING, "Luis", 6),
    ("Demande d'acces au repertoire partage", 1, 2, 2, PENDING, "Ana", 30),
    ("Wi-Fi instable dans la salle de reunion", 2, 1, 3, PROCESSING, "Priya", 4),
    ("Post-it : licence antivirus a renouveler", 3, 2, 2, NEW, "", 2),
    ("Lenteur sur le partagePublic", 2, 1, 4, NEW, "", 1),
    ("Erreur de synchronisation Exchange", 7, 1, 4, SOLVED, "Luis", 150),
    ("Demande de materiel (clavier)", 1, 2, 1, CLOSED, "Ana", 400),
]


# recherche Ticket : ids de listSearchOptions de GLPI 11 -> champs internes du simulateur.
# Le vrai GLPI renvoie des lignes {"<id colonne>": valeur} ; on l'imite pour que l'import soit
# valide sur l'instance reelle et sur la CI. La colonne 5 est l'id du technicien, pas son nom.
COLS = {"1": "name", "2": "id", "3": "priority", "5": "users_id_tech", "7": "itilcategories_name",
        "12": "status", "14": "type", "15": "date_creation", "16": "closedate", "17": "solvedate",
        "150": "takeintoaccount_delay_stat", "154": "solutiontime"}
# ids GLPI des colonnes de la table users_id_tech (User : 1 = nom, 2 = id)
USERS = {"2": "glpi", **{str(7 + i): n for i, n in enumerate(TECHS)}}
TECH_ID = {n: i for i, n in enumerate(TECHS, start=7)}

STATUS_LABEL = {NEW: "nouveau", PROCESSING: "en cours (traitement)", PLANNED: "planifie",
                PENDING: "en attente", SOLVED: "resolu", CLOSED: "cloture"}
PRIOS = [(1, "1 basse"), (2, "2 normale"), (3, "3 moyenne"), (4, "4 haute"), (5, "5 critique")]
CATS = sorted(CATEGORIES.items())


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _hms(seconds):
    if not seconds:
        return "-"
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def _sel(name, options, current):
    opts = "".join(f'<option value="{html.escape(str(v))}"{" selected" if str(v) == str(current) else ""}>'
                   f"{html.escape(str(label))}</option>" for v, label in options)
    return f'<select name="{name}">{opts}</select>'


CSS = """body{font:14px/1.4 system-ui,sans-serif;margin:24px;max-width:1150px;color:#1c2024}
h1{font-size:18px;margin:0 0 4px}p.meta{color:#6b7480;margin:0 0 16px}
table{border-collapse:collapse;width:100%;background:#fff}th,td{border:1px solid #dfe3e8;padding:5px 8px;text-align:left}
th{background:#f2f4f7}form.inline{display:flex;gap:4px;align-items:center}
input,select,button{font:inherit;padding:2px 4px}
form.new{background:#f7f9fb;border:1px solid #dfe3e8;padding:10px;margin:0 0 16px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
code{background:#f2f4f7;padding:1px 4px}"""


def build():
    rnd = random.Random(7)
    tickets = {}
    for i, (title, cat, typ, prio, status, tech, hours) in enumerate(SEED, start=1):
        opened = _now() - timedelta(hours=hours)
        taken = opened + timedelta(minutes=rnd.randint(4, 55)) if status != NEW else None
        solved = (taken + timedelta(minutes=rnd.randint(20, 300))
                  if taken and status in (SOLVED, CLOSED) else None)
        tickets[i] = {
            "id": i, "display_id": f"#{i}", "name": title, "content": title,
            "type": typ, "itilcategories_id": cat, "itilcategories_name": CATEGORIES.get(cat, CATEGORIES[1]),
            "priority": prio, "urgency": prio, "impact": 3, "status": status,
            "users_id_tech": tech, "date_creation": opened.strftime("%Y-%m-%d %H:%M:%S"),
            "takeintoaccount_delay_stat": int((taken - opened).total_seconds()) if taken else None,
            "solutiontime": int((solved - taken).total_seconds()) if solved else None,
            "solvedate": solved.strftime("%Y-%m-%d %H:%M:%S") if solved else None,
            "closedate": solved.strftime("%Y-%m-%d %H:%M:%S") if status == CLOSED else None,
            "date_mod": (solved or taken or opened).strftime("%Y-%m-%d %H:%M:%S"),
        }
    return tickets


TICKETS = build()
SESSIONS = {}


def _elapsed(t):
    """Recalcule les chronometres quand le helpdeck fait avancer le ticket."""
    opened = datetime.strptime(t["date_creation"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = _now()
    if t["status"] != NEW and not t["takeintoaccount_delay_stat"]:
        t["takeintoaccount_delay_stat"] = int((now - opened).total_seconds())
    if t["status"] in (SOLVED, CLOSED) and t["takeintoaccount_delay_stat"] and not t["solutiontime"]:
        t["solutiontime"] = max(0, int(now.timestamp() - opened.timestamp()) - t["takeintoaccount_delay_stat"])
    t["date_mod"] = now.strftime("%Y-%m-%d %H:%M:%S")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"     # Content-Length obligatoire pour keep-alive

    # -------------------------------------------------------------- console web (simulation)
    # ponytail: console de simulation sans authentification (outil local sur 127.0.0.1) ; l'API JSON
    # reste protegee par App-Token. Si un jour le simulateur est expose, mettre un jeton derriere /.
    def _send_html(self, code, body, headers=None):
        raw = f"<!doctype html><meta charset=utf-8><style>{CSS}</style>{body}".encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _console(self):
        cats = _sel("itilcategories_id", CATS, 1)
        prios = _sel("priority", PRIOS, 3)
        new = f"""<form class=new method=post action=/ui/ticket/new>
        <b>Simuler un ticket</b> titre <input name=name size=32 required> categorie {cats}
        priorite {prios} technicien {_sel("users_id_tech", [("", "aucun")] + [(t, t) for t in TECHS], "")}
        <button>Ouvrir le ticket</button></form>"""
        rows = []
        for t in sorted(TICKETS.values(), key=lambda t: t["id"]):
            rows.append(
                f"<tr><td>#{t['id']}</td><td>{html.escape(t['name'])}</td>"
                f"<td>{html.escape(t['itilcategories_name'])}</td><td>{t['priority']}</td>"
                f"<td>{STATUS_LABEL.get(t['status'], t['status'])}</td>"
                f"<td>{html.escape(t['users_id_tech']) or '&nbsp;-&nbsp;'}</td>"
                f"<td>{_hms(t['takeintoaccount_delay_stat'])}</td><td>{_hms(t['solutiontime'])}</td>"
                f"<td><form class=inline method=post action=/ui/ticket/{t['id']}>"
                f"{_sel('status', sorted(STATUS_LABEL.items()), t['status'])}"
                f"{_sel('users_id_tech', [("", 'aucun')] + [(x, x) for x in TECHS], t['users_id_tech'])}"
                f"<button>Enregistrer</button></form></td></tr>")
        return self._send_html(200, f"""<h1>GLPI simule — {len(TICKETS)} tickets</h1>
        <p class=meta>Console de simulation : ouvrir, affecter et resoudre des tickets, puis
        <code>./sirh start</code> + bouton « Synchroniser GLPI » cote SIRH.
        API : <a href=/glpi/apirest.php>/glpi/apirest.php</a> (App-Token requis).</p>
        {new}<table><tr><th>#</th><th>Titre</th><th>Categorie</th><th>Prio</th><th>Etat</th>
        <th>Technicien</th><th>MTTA</th><th>MTTR</th><th>Affecter / changer d'etat</th></tr>
        {"".join(rows)}</table>""")

    def _ui_post(self, parts, length):
        origin = self.headers.get("origin")
        if origin and urlparse(origin).netloc != self.headers.get("host"):
            return self._send_html(403, "<p>origine refusee</p>")
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        if parts == ["ui", "ticket", "new"]:
            new_id = max(TICKETS) + 1
            stamp = _now().strftime("%Y-%m-%d %H:%M:%S")
            cat = int(form.get("itilcategories_id", 1) or 1)
            TICKETS[new_id] = {
                "id": new_id, "display_id": f"#{new_id}", "name": (form.get("name") or "sans titre")[:120],
                "content": (form.get("name") or "sans titre")[:400], "type": 1 if cat == 1 else 2,
                "itilcategories_id": cat, "itilcategories_name": CATEGORIES.get(cat, CATEGORIES[1]),
                "priority": int(form.get("priority", 3) or 3), "urgency": int(form.get("priority", 3) or 3),
                "impact": 3, "status": NEW, "users_id_tech": form.get("users_id_tech", ""),
                "date_creation": stamp, "takeintoaccount_delay_stat": None, "solutiontime": None,
                "date_mod": stamp}
        elif len(parts) == 3 and parts[:2] == ["ui", "ticket"] and parts[2].isdigit() \
                and (t := TICKETS.get(int(parts[2]))):
            if "status" in form:
                t["status"] = int(form["status"])
            if "users_id_tech" in form:
                t["users_id_tech"] = form["users_id_tech"]
            _elapsed(t)
        return self._send_html(303, '<p>enregistre, <a href="/">retour</a></p>', {"Location": "/"})

    def log_message(self, fmt, *a):
        print(f"[glpi-sim] {self.address_string()} {fmt % a}", flush=True)

    # -------------------------------------------------------------- helpers
    def _send(self, code, payload, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, *msg):
        self._send(code, list(msg))

    def _authorized(self):
        """Comme GLPI : App-Token obligatoire s'il est configure, puis user_token ou Session-Token."""
        if APP_TOKEN and self.headers.get("App-Token") != APP_TOKEN:
            return self._error(401, "ERROR_WRONG_APP_TOKEN_PARAMETER", "App-Token invalide")
        auth = self.headers.get("Authorization", "")
        session = self.headers.get("Session-Token", "")
        if auth.startswith("user_token "):
            if auth.split(" ", 1)[1].strip() != USER_TOKEN:
                return self._error(401, "ERROR_GLPI_LOGIN_USER_TOKEN", "parameter user_token seems invalid")
            if not STRICT:
                return True
        if session and session in SESSIONS:
            return True
        return self._error(401, "ERROR_SESSION_TOKEN_MISSING", "Session-Token manquant")

    def _ticket_view(self, t, expand=False):
        out = dict(t)
        out["itilcategories_id"] = f"{CATEGORIES.get(t['itilcategories_id'], CATEGORIES[1])} ({t['itilcategories_id']})" if expand else t["itilcategories_id"]
        out["users_name"] = t["users_id_tech"] or " "
        if expand:
            out["date_creation"] = t["date_creation"]
        return out

    def _route(self):
        return [p for p in unquote(urlparse(self.path).path).split("/") if p]

    # -------------------------------------------------------------- verbes
    def do_GET(self):
        parts, q = self._route(), parse_qs(urlparse(self.path).query)
        if not parts or parts == ["glpi"]:        # console web de simulation
            return self._console()
        if parts[:2] == ["glpi", "apirest.php"] and len(parts) == 2:
            return self._send(200, {"name": "GLPI simule", "api": "v1 (apirest.php)",
                                    "endpoints": ["/initSession", "/search/Ticket", "/Ticket/{id}"]})
        rest = parts[2:]
        if not rest:
            return self._error(404, "endpoint inconnu")

        if rest == ["initSession"]:
            if APP_TOKEN and self.headers.get("App-Token") != APP_TOKEN:
                return self._error(401, "ERROR_WRONG_APP_TOKEN_PARAMETER", "App-Token invalide")
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("user_token ") or auth.split(" ", 1)[1].strip() != USER_TOKEN:
                return self._error(401, "ERROR_GLPI_LOGIN_USER_TOKEN", "parameter user_token seems invalid")
            token = secrets.token_hex(16)
            SESSIONS[token] = _now().isoformat()
            return self._send(200, {"session_token": token})

        if rest == ["killSession"]:
            SESSIONS.pop(self.headers.get("Session-Token", ""), None)
            return self._send(200, True)

        if not self._authorized():
            return None

        if rest == ["getGlpiConfig"]:
            return self._send(200, {"cfg_glpi": {"version": "11.0.0-sim", "url_base": "http://localhost"}})

        if rest == ["getMyProfiles"]:
            return self._send(200, {"myprofiles": [{"id": 4, "name": "Super-Admin"}]})

        if len(rest) == 1 and rest[0].lower() == "category":
            # les categories du helpdesk sont les classes partagees avec le support (SLA commun)
            return self._send(200, {"totalcount": len(CATEGORIES), "data": [
                {"id": i, "name": nom, "completename": nom, "color": CATEGORY_COLORS[i]}
                for i, nom in CATEGORIES.items()]})

        if len(rest) == 2 and rest[0].lower() == "search" and rest[1].lower() == "ticket":
            return self._search(q)

        if len(rest) == 2 and rest[0].lower() == "search" and rest[1].lower() == "user":
            # comme le vrai GLPI : lignes {"<id colonne>": valeur}, colonne 1 = nom, 2 = id
            wanted = q.get("forcedisplay[]") or ["1", "2"]
            lo, _, hi = q.get("range", ["0-99"])[0].partition("-")
            lo, hi = int(lo or 0), int(hi or lo)
            tous = [{"1": n, "2": i} for i, n in sorted(USERS.items(), key=lambda kv: int(kv[0]))]
            rows = [{c: r[c] for c in wanted if c in r} for r in tous][lo:hi + 1]
            return self._send(200, {"totalcount": len(tous), "count": len(rows), "data": rows})

        if len(rest) == 2 and rest[0].lower() == "ticket" and rest[1].isdigit():
            t = TICKETS.get(int(rest[1]))
            return self._send(200, self._ticket_view(t, expand=True)) if t else self._error(404, "Ticket introuvable")

        return self._error(404, "endpoint inconnu", "/".join(rest))

    def _search(self, q):
        """Moteur de recherche minimal : range, filter[<searchoption>] et criteria[i][...]."""
        cols = {"1": "name", "2": "id", "3": "priority", "12": "status", "15": "date_creation"}
        rows = list(TICKETS.values())
        crit = {}
        for key, values in q.items():
            m = re.match(r"criteria\[(\d+)\]\[(\w+)\]$", key)
            if m:
                crit.setdefault(int(m[1]), {})[m[2]] = values[0]
        for c in crit.values():
            col, val = cols.get(c.get("field", ""), "name"), c.get("value", "")
            stype = c.get("searchtype", "contains")
            if stype == "morethan" or val.startswith(">"):
                rows = [t for t in rows if str(t.get(col, "")) > val.lstrip(">")]
            elif stype == "equals":
                rows = [t for t in rows if str(t.get(col, "")) == val]
            else:
                rows = [t for t in rows if val.lower() in str(t.get(col, "")).lower()]
        for key, values in q.items():
            col = cols.get(key[7:-1]) if re.match(r"filter\[\d+\]$", key) else None
            if col and values[0].startswith(">"):
                rows = [t for t in rows if str(t.get(col, "")) > values[0][1:]]
        lo, _, hi = q.get("range", ["0-49"])[0].partition("-")
        lo, hi = int(lo or 0), int(hi or lo)
        page = rows[lo:hi + 1]
        # format GLPI : lignes {"<id colonne>": valeur} ; la colonne technicien porte un id
        wanted = q.get("forcedisplay[]") or list(COLS)

        def line(t):
            row = {}
            for c in wanted:
                v = t.get(COLS.get(c, ""), None)
                if c == "5" and v:
                    v = str(TECH_ID.get(v, ""))
                row[c] = v
            return row

        return self._send(200, {"totalcount": len(rows), "count": len(page),
                                "data": {str(t["id"]): line(t) for t in page}},
                          {"Content-Range": f"{lo}-{min(hi, len(rows) - 1)}/{len(rows)}",
                           "Accept-Range": str(len(TICKETS))})

    def do_POST(self):
        # mise a jour d'un ticket : GLPI accepte PUT ou PATCH sur /Ticket/{id}
        parts = self._route()
        if parts[:1] == ["ui"]:                  # formulaires de la console
            return self._ui_post(parts, int(self.headers.get("Content-Length") or 0))
        rest = parts[2:]
        if not self._authorized():
            return None
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._error(400, "JSON invalide")
        if len(rest) == 2 and rest[0].lower() == "ticket" and rest[1].isdigit():
            t = TICKETS.get(int(rest[1]))
            if not t:
                return self._error(404, "Ticket introuvable")
            inp = payload.get("input", payload)
            data = (inp[0] if isinstance(inp, list) else inp) or {}
            if "users_id_tech" in data or "users_name" in data:
                tech = str(data.get("users_id_tech", data.get("users_name", ""))).strip()
                t["users_id_tech"] = "" if tech in ("", " ") else tech
            if "status" in data:
                t["status"] = int(data["status"])
            _elapsed(t)
            return self._send(200, [{str(t["id"]): True, "message": ""}])
        return self._error(404, "endpoint inconnu")


    do_PUT = do_POST
    do_PATCH = do_POST


if __name__ == "__main__":
    print(f"[glpi-sim] {len(TICKETS)} tickets de simulation sur http://0.0.0.0:{PORT}/glpi/apirest.php "
          f"(app-token={APP_TOKEN}, user-token={USER_TOKEN})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
