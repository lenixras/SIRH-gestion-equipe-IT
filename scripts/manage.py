"""CLI d'exploitation : creation du premier compte et import des KPI tickets.

    python3 -m scripts.manage create-user --name Marc --email marc@x.fr [--role manager]
    python3 -m scripts.manage import-kpi --json tickets.json      # fichier normalise
    python3 -m scripts.manage import-kpi --glpi                    # GLPI REST (GLPI_URL/GLPI_TOKEN)

La lecture GLPI et l'upsert sont dans app/glpi.py (partages avec l'ecran KPI et les tests).
"""
import argparse
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, glpi  # noqa: E402
from app.auth import hash_password  # noqa: E402


def create_user(args):
    db.init()
    # le role vient de la table (les roles sont creatables) : une liste en dur refuserait les roles metier
    if not db.one("SELECT 1 FROM roles WHERE name = %s", (args.role,)):
        sys.exit(f"role inconnu : {args.role} (crees-le dans l'ecran Roles, ou dans schema.sql)")
    with db.connect() as c:
        row = c.execute(
            "INSERT INTO persons (name, email, title, role, glpi_id, password_hash) VALUES (%s,%s,%s,%s,%s,%s)"
            " RETURNING id, name, email, role, glpi_id",
            (args.name, args.email, args.title, args.role, getattr(args, "glpi_id", None),
             hash_password(args.password))
        ).fetchone()
    print(f"cree : {row['id']} {row['name']} <{row['email']}> ({row['role']}, glpi {row['glpi_id']})")


def import_kpi(args):
    if args.glpi:
        n = glpi.sync()
        print(f"{n} tickets glpi importes (upsert sur source+external_id)")
        return
    with open(args.json) as f:
        tickets = json.load(f)
    db.init()
    print(f"{glpi.import_tickets(tickets, args.source)} tickets {args.source} importes"
          " (upsert sur source+external_id)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("create-user")
    u.add_argument("--name", required=True)
    u.add_argument("--email", required=True)
    u.add_argument("--title")
    u.add_argument("--role", default="tech", help="nom d'un role existant (defaut tech)")
    u.add_argument("--glpi-id", type=int, help="identifiant du technicien dans l'outil de tickets")
    u.add_argument("--password")
    u.set_defaults(fn=create_user)

    k = sub.add_parser("import-kpi")
    k.add_argument("--json", help="fichier de tickets normalises")
    k.add_argument("--glpi", action="store_true", help="lecture de l'API GLPI")
    k.add_argument("--source", default="fichier")
    k.set_defaults(fn=import_kpi)

    args = ap.parse_args()
    if args.cmd == "create-user" and not args.password:
        args.password = getpass.getpass("mot de passe : ")
    args.fn(args)


if __name__ == "__main__":
    main()
