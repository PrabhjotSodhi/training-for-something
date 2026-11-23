"""Scores made from day facts."""

import json
import sqlite3

from tfs.scores import load, readiness

_VERSIONS = {
    readiness.SCORE: readiness.FORMULA_VERSION,
    load.SCORE: load.FORMULA_VERSION,
    load.CARDIO_SCORE: load.CARDIO_FORMULA_VERSION,
}


def rebuild(conn: sqlite3.Connection) -> None:
    """Recompute every score. The caller owns the transaction."""
    readiness.rebuild(conn)
    load.rebuild(conn)
    load.rebuild_cardio(conn)


def rebuild_if_outdated(conn: sqlite3.Connection) -> None:
    """Recompute the scores when any stored row has another formula version than its score's.

    Cardio load with no rows is also recomputed, so an upgrade fills its history. The caller owns the transaction.
    """
    if conn.execute(
        "SELECT 1 FROM scores WHERE formula_version != json_extract(?, '$.' || score) LIMIT 1",
        (json.dumps(_VERSIONS),),
    ).fetchone():
        rebuild(conn)
    elif not conn.execute("SELECT 1 FROM scores WHERE score = ?", (load.CARDIO_SCORE,)).fetchone():
        load.rebuild_cardio(conn)
