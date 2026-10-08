import sqlite3
import json
from pathlib import Path


# ============================================================
# PROJECT PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data"

DATABASE_PATH = DATA_DIR / "newsroom.db"


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_connection():
    """
    Create a connection to the SQLite database.
    """

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    connection = sqlite3.connect(
        DATABASE_PATH
    )

    connection.row_factory = sqlite3.Row

    return connection


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def initialize_database():
    """
    Create and upgrade all required database tables.
    """

    connection = get_connection()

    cursor = connection.cursor()

    # --------------------------------------------------------
    # SAVED ARTICLES TABLE
    # --------------------------------------------------------

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS saved_articles (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            article_title TEXT UNIQUE NOT NULL,

            description TEXT,

            url TEXT,

            source TEXT,

            published_at TEXT,

            saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

        )
        """
    )

    # --------------------------------------------------------
    # LOCAL USER ACCOUNTS AND SIGN-IN SESSIONS
    # --------------------------------------------------------

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS user_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS user_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES user_accounts(id) ON DELETE CASCADE
        )
        """
    )

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_sessions_user_id ON user_sessions(user_id)"
    )

    # --------------------------------------------------------
    # SUMMARIES TABLE
    # --------------------------------------------------------

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS summaries (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            article_title TEXT UNIQUE NOT NULL,

            summary TEXT NOT NULL,

            key_points TEXT NOT NULL,

            why_it_matters TEXT NOT NULL,

            importance_score INTEGER DEFAULT 0,

            importance_level TEXT DEFAULT 'UNKNOWN',

            sentiment TEXT DEFAULT 'NEUTRAL',

            who_is_affected TEXT DEFAULT '',

            what_happens_next TEXT DEFAULT '',

            career_impact TEXT DEFAULT '',

            career_level TEXT DEFAULT 'LOW',

            skills_to_learn TEXT DEFAULT '[]',

            relevant_job_roles TEXT DEFAULT '[]',

            career_priority TEXT DEFAULT 'LOW',

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

        )
        """
    )

    # --------------------------------------------------------
    # SAFE MIGRATION FOR EXISTING DATABASES
    # --------------------------------------------------------

    existing_columns = set()

    cursor.execute(
        "PRAGMA table_info(summaries)"
    )

    for row in cursor.fetchall():

        existing_columns.add(
            row["name"]
        )

    columns_to_add = {

        "importance_score":
            "INTEGER DEFAULT 0",

        "importance_level":
            "TEXT DEFAULT 'UNKNOWN'",

        "sentiment":
            "TEXT DEFAULT 'NEUTRAL'",

        "who_is_affected":
            "TEXT DEFAULT ''",

        "what_happens_next":
            "TEXT DEFAULT ''",

        "career_impact":
            "TEXT DEFAULT ''",

        "career_impact_score":
            "INTEGER DEFAULT 0",

        "career_level":
            "TEXT DEFAULT 'LOW'",

        "skills_to_learn":
            "TEXT DEFAULT '[]'",

        "relevant_job_roles":
            "TEXT DEFAULT '[]'",

        "career_priority":
            "TEXT DEFAULT 'LOW'",

        "career_opportunities":
            "TEXT DEFAULT ''",

        "who_should_care":
            "TEXT DEFAULT ''"
    }
    for column_name, column_definition in columns_to_add.items():

        if column_name not in existing_columns:

            cursor.execute(
                f"""
                ALTER TABLE summaries
                ADD COLUMN {column_name} {column_definition}
                """
            )

    connection.commit()

    connection.close()

    print("DATABASE INITIALIZED")



# ============================================================
# JOBS INTELLIGENCE CACHE
# ============================================================

def init_jobs_intelligence_cache():
    """
    Create a separate cache table for Jobs Career Intelligence.
    """

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs_intelligence_cache (
            cache_key TEXT PRIMARY KEY,
            result TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    connection.commit()
    connection.close()


def get_cached_jobs_intelligence(cache_key):
    """
    Return cached Jobs Intelligence if available.
    """

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT result
        FROM jobs_intelligence_cache
        WHERE cache_key = ?
        """,
        (cache_key,)
    )

    row = cursor.fetchone()

    connection.close()

    if row is None:
        return None

    try:
        return json.loads(row["result"])
    except Exception:
        return None


def save_jobs_intelligence(cache_key, result):
    """
    Save Jobs Intelligence result to cache.
    """

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        """
        INSERT OR REPLACE INTO jobs_intelligence_cache
        (
            cache_key,
            result
        )
        VALUES (?, ?)
        """,
        (
            cache_key,
            json.dumps(result, ensure_ascii=False)
        )
    )

    connection.commit()
    connection.close()


# ============================================================
# GET CACHED SUMMARY
# ============================================================

def get_cached_summary(title):
    """
    Return a previously generated AI analysis.
    """

    connection = get_connection()

    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT
            summary,
            key_points,
            why_it_matters,
            importance_score,
            importance_level,
            sentiment,
            who_is_affected,
            what_happens_next,
            career_impact,
            career_level,
            skills_to_learn,
            relevant_job_roles,
            career_priority
        FROM summaries
        WHERE article_title = ?
        """,
        (title,)
    )

    row = cursor.fetchone()

    connection.close()

    if row is None:

        return None

    try:
        key_points = json.loads(
            row["key_points"]
        )
    except Exception:

        key_points = []

    try:
        skills_to_learn = json.loads(
            row["skills_to_learn"]
        )
    except Exception:

        skills_to_learn = []

    try:
        relevant_job_roles = json.loads(
            row["relevant_job_roles"]
        )
    except Exception:

        relevant_job_roles = []

    return {

        "summary":
            row["summary"],

        "key_points":
            key_points,

        "why_it_matters":
            row["why_it_matters"],

        "importance_score":
            row["importance_score"] or 0,

        "importance_level":
            row["importance_level"] or "UNKNOWN",

        "sentiment":
            row["sentiment"] or "NEUTRAL",

        "who_is_affected":
            row["who_is_affected"] or "",

        "what_happens_next":
            row["what_happens_next"] or "",

        "career_impact":
            row["career_impact"] or "",

        "career_level":
            row["career_level"] or "LOW",

        "skills_to_learn":
            skills_to_learn,

        "relevant_job_roles":
            relevant_job_roles,

        "career_priority":
            row["career_priority"] or "LOW",

        "cached":
            True
    }


# ============================================================
# SAVE SUMMARY
# ============================================================

def save_summary(title, result):
    """
    Store the complete Ollama analysis.
    """

    connection = get_connection()
    cursor = connection.cursor()

    key_points = result.get(
        "key_points",
        []
    )

    skills_to_learn = result.get(
        "skills_to_learn",
        []
    )

    relevant_job_roles = result.get(
        "relevant_job_roles",
        []
    )

    cursor.execute(
        """
        INSERT OR REPLACE INTO summaries
        (
            article_title,
            summary,
            key_points,
            why_it_matters,
            importance_score,
            importance_level,
            sentiment,
            who_is_affected,
            what_happens_next,
            career_impact,
            career_impact_score,
            career_level,
            skills_to_learn,
            relevant_job_roles,
            career_priority,
            career_opportunities,
            who_should_care
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title,

            result.get(
                "summary",
                "No summary available."
            ),

            json.dumps(
                key_points,
                ensure_ascii=False
            ),

            result.get(
                "why_it_matters",
                "No additional information available."
            ),

            int(
                result.get(
                    "importance_score",
                    0
                ) or 0
            ),

            result.get(
                "importance_level",
                "UNKNOWN"
            ),

            result.get(
                "sentiment",
                "NEUTRAL"
            ),

            result.get(
                "who_is_affected",
                ""
            ),

            result.get(
                "what_happens_next",
                ""
            ),

            result.get(
                "career_impact",
                ""
            ),

            int(
                result.get(
                    "career_impact_score",
                    0
                ) or 0
            ),

            result.get(
                "career_level",
                "LOW"
            ),

            json.dumps(
                skills_to_learn,
                ensure_ascii=False
            ),

            json.dumps(
                relevant_job_roles,
                ensure_ascii=False
            ),

            result.get(
                "career_priority",
                "LOW"
            ),

            result.get(
                "career_opportunities",
                ""
            ),

            result.get(
                "who_should_care",
                ""
            )
        )
    )

    connection.commit()
    connection.close()
# ============================================================
# CLEAR AI CACHE
# ============================================================

def clear_summary_cache():
    """
    Delete previously cached AI analyses.

    This is useful after changing the AI prompt or
    analysis structure.
    """

    connection = get_connection()

    cursor = connection.cursor()

    cursor.execute(
        "DELETE FROM summaries"
    )

    deleted_count = cursor.rowcount

    connection.commit()

    connection.close()

    return deleted_count
