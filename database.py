# database.py
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Dict, Any, Iterable


# Frozen schema version for v1.0.0-alpha (Stage 1: Data/Core Freeze)
SCHEMA_VERSION = 1


def _default_db_path() -> Path:
    # Runtime file (not source): movies.db in project root
    return Path(__file__).resolve().parent / "movies.db"


def connect(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else _default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    return conn


@contextmanager
def db_session(db_path: Optional[str | Path] = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA temp_store = MEMORY;")
    conn.execute("PRAGMA cache_size = -20000;")  # ~20MB (KB units)
    conn.execute("PRAGMA busy_timeout = 5000;")


# -------------------------------
# Migration helpers (SAFE)
# -------------------------------
def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table});").fetchall()
    return {str(r["name"]) for r in rows} if rows else set()


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    col: str,
    col_def_sql: str,
) -> None:
    cols = _table_columns(conn, table)
    if col in cols:
        return
    # SQLite: ALTER TABLE ... ADD COLUMN ... is supported
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def_sql};")


def _create_index_if_possible(
    conn: sqlite3.Connection,
    index_sql: str,
    required_table: str,
    required_cols: Iterable[str],
) -> None:
    cols = _table_columns(conn, required_table)
    if all(c in cols for c in required_cols):
        conn.execute(index_sql)


def _migrate(conn: sqlite3.Connection) -> None:
    """
    Bring older DBs forward safely. No destructive changes.
    This runs even when tables already exist.
    """
    # If movies table exists, ensure critical columns exist.
    cols = _table_columns(conn, "movies")
    if not cols:
        return

    # Older DBs may miss date_added -> required by current code & indices.
    # Use DEFAULT 0 to satisfy NOT NULL.
    _add_column_if_missing(conn, "movies", "date_added", "INTEGER NOT NULL DEFAULT 0")

    # Future-proof: some code paths use imdb_id; keep it nullable.
    _add_column_if_missing(conn, "movies", "imdb_id", "TEXT")


def init_db(conn: sqlite3.Connection) -> None:
    """Initialize schema if missing. Safe to call multiple times."""
    _create_meta(conn)
    _create_tables(conn)
    _migrate(conn)          # ✅ ensure columns exist BEFORE indices
    _create_indices(conn)
    _set_schema_version(conn, SCHEMA_VERSION)


def get_schema_version(conn: sqlite3.Connection) -> int:
    _create_meta(conn)
    row = conn.execute("SELECT schema_version FROM schema_meta WHERE id = 1;").fetchone()
    return int(row["schema_version"]) if row else 0


def _create_meta(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            schema_version INTEGER NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO schema_meta (id, schema_version)
        VALUES (1, 0)
        ON CONFLICT(id) DO NOTHING;
        """
    )


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        """
        UPDATE schema_meta
        SET schema_version = ?
        WHERE id = 1;
        """,
        (int(version),),
    )


def _create_tables(conn: sqlite3.Connection) -> None:
    # IMPORTANT: create referenced tables first (genres, people), then movies, then join tables.

    # genres
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS genres (
            id INTEGER PRIMARY KEY,
            tmdb_genre_id INTEGER NOT NULL UNIQUE,
            name TEXT NOT NULL
        );
        """
    )

    # people
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY,
            tmdb_person_id INTEGER NOT NULL UNIQUE,
            name TEXT NOT NULL,
            profile_path TEXT
        );
        """
    )

    # movies
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS movies (
            id INTEGER PRIMARY KEY,

            tmdb_id INTEGER NOT NULL UNIQUE,

            title TEXT NOT NULL,
            year INTEGER,
            runtime INTEGER,
            overview TEXT,

            poster_path TEXT,
            backdrop_path TEXT,

            lang_original TEXT,

            color_mode TEXT NOT NULL DEFAULT 'unknown'
                CHECK (color_mode IN ('unknown','color','bw')),

            edition TEXT NOT NULL DEFAULT 'Standard',

            user_rating INTEGER
                CHECK (user_rating IS NULL OR (user_rating >= 0 AND user_rating <= 5)),

            watched INTEGER NOT NULL DEFAULT 0
                CHECK (watched IN (0,1)),

            date_added INTEGER NOT NULL,

            studio TEXT,

            tmdb_rating REAL,
            tmdb_votes INTEGER,

            primary_genre_id INTEGER,

            -- Optional / forward-compatible
            imdb_id TEXT,

            -- Operational fields for Scan/Rescan (approved)
            file_path TEXT UNIQUE,
            file_mtime INTEGER,
            file_size INTEGER,

            -- Table constraints MUST be after all column definitions
            FOREIGN KEY (primary_genre_id) REFERENCES genres(id) ON DELETE SET NULL
        );
        """
    )

    # movie_genres (many-to-many)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS movie_genres (
            movie_id INTEGER NOT NULL,
            genre_id INTEGER NOT NULL,
            pos INTEGER,

            PRIMARY KEY (movie_id, genre_id),

            FOREIGN KEY (movie_id) REFERENCES movies(id) ON DELETE CASCADE,
            FOREIGN KEY (genre_id) REFERENCES genres(id) ON DELETE CASCADE
        );
        """
    )

    # movie_cast
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS movie_cast (
            movie_id INTEGER NOT NULL,
            person_id INTEGER NOT NULL,
            role TEXT,
            cast_order INTEGER NOT NULL,

            PRIMARY KEY (movie_id, person_id, cast_order),

            FOREIGN KEY (movie_id) REFERENCES movies(id) ON DELETE CASCADE,
            FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE CASCADE
        );
        """
    )

    # movie_crew (only the 4 MVP jobs)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS movie_crew (
            movie_id INTEGER NOT NULL,
            person_id INTEGER NOT NULL,
            job TEXT NOT NULL
                CHECK (job IN ('director','producer','writer','musician')),
            dept TEXT,

            PRIMARY KEY (movie_id, person_id, job),

            FOREIGN KEY (movie_id) REFERENCES movies(id) ON DELETE CASCADE,
            FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE CASCADE
        );
        """
    )


def _create_indices(conn: sqlite3.Connection) -> None:
    # movies sorting / filtering indices
    _create_index_if_possible(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_movies_title ON movies(title COLLATE NOCASE);",
        "movies",
        ["title"],
    )
    _create_index_if_possible(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_movies_year ON movies(year);",
        "movies",
        ["year"],
    )
    _create_index_if_possible(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_movies_user_rating ON movies(user_rating);",
        "movies",
        ["user_rating"],
    )
    _create_index_if_possible(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_movies_date_added ON movies(date_added);",
        "movies",
        ["date_added"],
    )
    _create_index_if_possible(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_movies_watched ON movies(watched);",
        "movies",
        ["watched"],
    )
    _create_index_if_possible(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_movies_primary_genre ON movies(primary_genre_id);",
        "movies",
        ["primary_genre_id"],
    )

    # join helper indices
    _create_index_if_possible(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_movie_genres_movie ON movie_genres(movie_id);",
        "movie_genres",
        ["movie_id"],
    )
    _create_index_if_possible(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_movie_genres_genre ON movie_genres(genre_id);",
        "movie_genres",
        ["genre_id"],
    )
    _create_index_if_possible(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_movie_cast_movie_order ON movie_cast(movie_id, cast_order);",
        "movie_cast",
        ["movie_id", "cast_order"],
    )
    _create_index_if_possible(
        conn,
        "CREATE INDEX IF NOT EXISTS idx_movie_crew_movie_job ON movie_crew(movie_id, job);",
        "movie_crew",
        ["movie_id", "job"],
    )


def ensure_db(db_path: Optional[str | Path] = None) -> None:
    with db_session(db_path) as conn:
        init_db(conn)


# ---------------------------------------------------------------------
# REQUIRED by main.py (v1.0.0-alpha.5): database.insert_movie_from_tmdb
# ---------------------------------------------------------------------
def insert_movie_from_tmdb(
    db_path: str | Path,
    tm: Any,
    file_path: str,
    file_mtime: Optional[int],
    file_size: Optional[int],
    poster_local: Optional[str],
    backdrop_local: Optional[str],
    people_profiles: Dict[int, Optional[str]],
) -> None:
    """
    Insert a TMDB movie + related rows according to the frozen schema (SCHEMA_VERSION=1).

    Expected `tm` fields (best-effort):
      tm.tmdb_id, tm.title, tm.year, tm.runtime, tm.overview,
      tm.poster_path, tm.backdrop_path,
      tm.genres -> [(tmdb_genre_id, name), ...]
      tm.cast -> [(pid, name, role, profile_path), ...]
      tm.crew -> [(pid, name, job, profile_path), ...]
      tm.studio, tm.tmdb_rating, tm.tmdb_votes,
      tm.original_language (optional)
    """
    now_utc = int(time.time())

    with db_session(db_path) as conn:
        # 1) Genres upsert + map
        genre_id_map: Dict[int, int] = {}
        for pos, (tmdb_gid, gname) in enumerate(getattr(tm, "genres", []) or []):
            conn.execute(
                """
                INSERT INTO genres (tmdb_genre_id, name)
                VALUES (?, ?)
                ON CONFLICT(tmdb_genre_id) DO UPDATE SET name=excluded.name;
                """,
                (int(tmdb_gid), str(gname)),
            )
            row = conn.execute("SELECT id FROM genres WHERE tmdb_genre_id = ?;", (int(tmdb_gid),)).fetchone()
            if row:
                genre_id_map[int(tmdb_gid)] = int(row["id"])

        primary_genre_id: Optional[int] = None
        genres_list = getattr(tm, "genres", []) or []
        if genres_list:
            try:
                primary_genre_id = genre_id_map.get(int(genres_list[0][0]))
            except Exception:
                primary_genre_id = None

        # 2) Insert movie
        conn.execute(
            """
            INSERT INTO movies (
                tmdb_id, title, year, runtime, overview,
                poster_path, backdrop_path,
                lang_original, color_mode, edition, user_rating, watched,
                date_added,
                studio, tmdb_rating, tmdb_votes,
                primary_genre_id,
                imdb_id,
                file_path, file_mtime, file_size
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                int(getattr(tm, "tmdb_id")),
                str(getattr(tm, "title") or ""),
                getattr(tm, "year", None),
                getattr(tm, "runtime", None),
                getattr(tm, "overview", None),
                poster_local,
                backdrop_local,
                getattr(tm, "original_language", None),
                "unknown",
                "Standard",
                0,
                0,
                now_utc,
                getattr(tm, "studio", None),
                getattr(tm, "tmdb_rating", None),
                getattr(tm, "tmdb_votes", None),
                primary_genre_id,
                getattr(tm, "imdb_id", None),
                file_path,
                file_mtime,
                file_size,
            ),
        )

        movie_row = conn.execute("SELECT id FROM movies WHERE tmdb_id = ?;", (int(getattr(tm, "tmdb_id")),)).fetchone()
        if not movie_row:
            return
        movie_id = int(movie_row["id"])

        # 3) movie_genres
        for pos, (tmdb_gid, _gname) in enumerate(genres_list):
            gid = genre_id_map.get(int(tmdb_gid))
            if gid:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO movie_genres (movie_id, genre_id, pos)
                    VALUES (?, ?, ?);
                    """,
                    (movie_id, gid, int(pos)),
                )

        # 4) Cast (top 12)
        cast_list = getattr(tm, "cast", []) or []
        for cast_order, (pid, name, role, _profile_path) in enumerate(cast_list[:12]):
            conn.execute(
                """
                INSERT INTO people (tmdb_person_id, name, profile_path)
                VALUES (?, ?, ?)
                ON CONFLICT(tmdb_person_id) DO UPDATE SET
                    name=excluded.name,
                    profile_path=COALESCE(excluded.profile_path, people.profile_path);
                """,
                (int(pid), str(name), people_profiles.get(int(pid))),
            )
            prow = conn.execute("SELECT id FROM people WHERE tmdb_person_id = ?;", (int(pid),)).fetchone()
            if not prow:
                continue
            person_id = int(prow["id"])
            conn.execute(
                """
                INSERT OR IGNORE INTO movie_cast (movie_id, person_id, role, cast_order)
                VALUES (?, ?, ?, ?);
                """,
                (movie_id, person_id, str(role) if role else None, int(cast_order)),
            )

        # 5) Crew (only 4 MVP jobs)
        crew_list = getattr(tm, "crew", []) or []
        for (pid, name, job, dept, _profile_path) in crew_list:
            job_norm = str(job).strip().lower()
            if job_norm not in ("director", "producer", "writer", "musician"):
                continue

            conn.execute(
                """
                INSERT INTO people (tmdb_person_id, name, profile_path)
                VALUES (?, ?, ?)
                ON CONFLICT(tmdb_person_id) DO UPDATE SET
                    name=excluded.name,
                    profile_path=COALESCE(excluded.profile_path, people.profile_path);
                """,
                (int(pid), str(name), people_profiles.get(int(pid))),
            )
            prow = conn.execute("SELECT id FROM people WHERE tmdb_person_id = ?;", (int(pid),)).fetchone()
            if not prow:
                continue
            person_id = int(prow["id"])
            conn.execute(
                """
                INSERT OR IGNORE INTO movie_crew (movie_id, person_id, job, dept)
                VALUES (?, ?, ?, ?);
                """,
                (movie_id, person_id, job_norm, str(dept) if dept else None),
            )
