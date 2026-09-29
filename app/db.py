"""Connexion Postgres. Une connexion par requete : pas de pool (5-20 utilisateurs)."""
import os
import pathlib

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get("DATABASE_URL", "postgresql://sirh:sirh@localhost:55432/sirh")
ROOT = pathlib.Path(__file__).resolve().parent.parent


def connect(**kw):
    return psycopg.connect(DSN, row_factory=dict_row, autocommit=True, **kw)


def async_listen(dsn):  # écoute des NOTIFY (synchronisation dynamique du dashboard)
    return psycopg.AsyncConnection.connect(dsn, autocommit=True)


def init():
    with connect() as conn, open(ROOT / "schema.sql") as f:
        conn.execute(f.read())


def query(sql, params=(), *, timeout_ms=5000, conn=None):
    """SELECT avec statement_timeout : une requete qui derape ne bloque pas l'app."""
    if conn is not None:
        return conn.execute(sql, params).fetchall()
    with connect() as c:
        c.execute("SELECT set_config('statement_timeout', %s, false)", (str(timeout_ms),))
        return c.execute(sql, params).fetchall()


def one(sql, params=(), **kw):
    rows = query(sql, params, **kw)
    return rows[0] if rows else None
