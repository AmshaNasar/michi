"""Postgres connection handling."""

import contextlib
import os
from typing import Iterator

import psycopg2
import psycopg2.extras
from psycopg2.extensions import connection as Connection

from twin.config import SETTINGS

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


@contextlib.contextmanager
def connect() -> Iterator[Connection]:
    """Yield a connection, committing on success and rolling back on error."""
    conn = psycopg2.connect(SETTINGS.database_url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextlib.contextmanager
def cursor():
    """Yield a dict-returning cursor inside a managed transaction."""
    with connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur


def apply_schema() -> None:
    """Create tables if they don't exist. Safe to run repeatedly."""
    with open(SCHEMA_PATH, "r") as handle:
        sql = handle.read()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
