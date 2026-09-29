"""Auth locale : pbkdf2 (stdlib) + cookie de session (navigateur) + HTTP Basic (API/MCP).
RBAC : un role porte des permissions nommees (table role_permissions) ; la propriete d'une ligne
reste une regle de code, pas une permission -- une permission est un droit statique.
"""
import hashlib
import hmac
import os
import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import db

REALM = "Mini-SIRH equipe"
COOKIE = "sirh_session"
SESSION_TTL = "12 h"
basic = HTTPBasic(realm=REALM, auto_error=False)   # defi nous-meme : message FR + realm
CHALLENGE = {"WWW-Authenticate": f'Basic realm="{REALM}"'}
DEFAULT_ROUNDS = 200_000


class Denied(Exception):
    """Ecriture refusee : pas les droits, ou cible non modifiable par l'acteur."""


def hash_password(password, rounds=DEFAULT_ROUNDS):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds).hex()
    return f"pbkdf2${rounds}${salt.hex()}${digest}"


def verify_password(password, stored):
    try:
        algo, rounds, salt, digest = stored.split("$")
        if algo != "pbkdf2":
            return False
        expect = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expect, digest)


def person_by_email(email):
    return db.one("SELECT * FROM persons WHERE lower(email) = lower(%s) AND active", (email,))


# -------------------------------------------------------------- permissions

def perms(actor):
    """Permissions du role de l'acteur. Une requete par appel HTTP : 5-20 utilisateurs."""
    # ponytail: une seule requete par verification ; passer par un cache (functools) par role
    # seulement si le volume d'appels devient un probleme mesure.
    if not actor:
        return frozenset()
    return frozenset(r["perm"] for r in db.query(
        "SELECT perm FROM role_permissions WHERE role = %s", (actor["role"],)))


def require(actor, perm):
    if perm not in perms(actor):
        raise Denied(f"permission requise : {perm}")
    return actor


def check_person_access(actor, person_id, any_perm=None):
    """Droit limite a sa propre fiche, sauf si l'acteur tient `any_perm` (ecriture en cascade)."""
    if int(actor["id"]) == int(person_id) or (any_perm and any_perm in perms(actor)):
        return
    raise Denied("droit limite a sa propre fiche")


# -------------------------------------------------------------- sessions

def open_session(person_id):
    """Ouvre une session navigateur et purge les perimees au passage."""
    token = secrets.token_urlsafe(32)
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < now()")
        conn.execute("INSERT INTO sessions (token, person_id, expires_at) VALUES (%s, %s, now() + %s::interval)",
                     (token, person_id, SESSION_TTL))
    return token


def close_session(token):
    if token:
        with db.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token = %s", (token,))


def actor_from_token(token):
    return db.one("SELECT pe.* FROM sessions s JOIN persons pe ON pe.id = s.person_id"
                  " WHERE s.token = %s AND s.expires_at > now() AND pe.active", (token,))


# -------------------------------------------------------------- dependances

async def current_user(request: Request, cred: HTTPBasicCredentials = Depends(basic)):
    """Cookie d'abord (navigateur), puis Basic : l'API et les tests continuent de passer par Basic."""
    actor = actor_from_token(request.cookies.get(COOKIE)) if request else None
    if actor is None:
        if cred is None:
            raise HTTPException(401, "identifiants requis (session ou email + mot de passe)", headers=CHALLENGE)
        actor = person_by_email(cred.username)
        if not actor or not verify_password(cred.password, actor["password_hash"]):
            raise HTTPException(401, "identifiants invalides")   # le realm est renvoye par le dependance
    return actor


def needs(perm):
    """Dependance FastAPI : 403 si l'acteur n'a pas la permission."""
    async def dep(actor=Depends(current_user)):
        try:
            return require(actor, perm)
        except Denied as e:
            raise HTTPException(403, str(e))
    return dep


def mcp_actor():
    # ponytail: une seule identite service pour les ecritures MCP (pas de token par personne) ;
    # passer l'email de l'acteur reel si un chat multi-utilisateurs apparait.
    email = os.environ.get("MCP_ACTOR_EMAIL", "manager@example.com")
    actor = person_by_email(email)
    if not actor:
        raise Denied(f"MCP_ACTOR_EMAIL inconnu : {email}")
    return actor
