import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from logger import get_logger

log = get_logger("database")

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS projects (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    collection TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repositories (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL,
    name           TEXT NOT NULL,
    default_branch TEXT,
    last_synced    TEXT
);

CREATE TABLE IF NOT EXISTS commits (
    id               TEXT PRIMARY KEY,
    repo_id          TEXT NOT NULL,
    author_email     TEXT,
    author_name      TEXT,
    author_date      TEXT,
    committer_email  TEXT,
    committer_name   TEXT,
    committer_date   TEXT,
    comment          TEXT,
    changes_add      INTEGER DEFAULT 0,
    changes_edit     INTEGER DEFAULT 0,
    changes_delete   INTEGER DEFAULT 0,
    is_merge         INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pull_requests (
    id            INTEGER PRIMARY KEY,
    repo_id       TEXT NOT NULL,
    project_id    TEXT NOT NULL,
    title         TEXT,
    creator_email TEXT,
    creator_name  TEXT,
    status        TEXT,
    created_date  TEXT,
    closed_date   TEXT,
    target_branch TEXT,
    source_branch TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    repo_id   TEXT,
    type      TEXT,
    count     INTEGER DEFAULT 0,
    error     TEXT
);

CREATE TABLE IF NOT EXISTS employee_aliases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    primary_email TEXT NOT NULL,
    alias_email   TEXT NOT NULL,
    created_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(alias_email)
);

CREATE TABLE IF NOT EXISTS pr_reviews (
    pr_id          INTEGER NOT NULL,
    reviewer_email TEXT NOT NULL,
    reviewer_name  TEXT,
    vote           INTEGER DEFAULT 0,
    PRIMARY KEY (pr_id, reviewer_email)
);

CREATE TABLE IF NOT EXISTS teams (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS team_members (
    team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    email   TEXT NOT NULL,
    PRIMARY KEY (team_id, email)
);

CREATE TABLE IF NOT EXISTS author_display_names (
    email        TEXT PRIMARY KEY,
    display_name TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_commits_author_date  ON commits(author_email, author_date);
CREATE INDEX IF NOT EXISTS idx_commits_repo_date    ON commits(repo_id, author_date);
CREATE INDEX IF NOT EXISTS idx_commits_date         ON commits(author_date);
CREATE INDEX IF NOT EXISTS idx_pr_creator_date      ON pull_requests(creator_email, created_date);
CREATE INDEX IF NOT EXISTS idx_pr_repo              ON pull_requests(repo_id);
CREATE INDEX IF NOT EXISTS idx_aliases_primary      ON employee_aliases(primary_email);
CREATE INDEX IF NOT EXISTS idx_pr_reviews_email     ON pr_reviews(reviewer_email);
CREATE INDEX IF NOT EXISTS idx_pr_reviews_pr        ON pr_reviews(pr_id);
"""

# Deterministic login → canonical_email map.
# A person can commit under several emails sharing one NT login (e.g.
# makarov_am@w1458w10 for real work plus a stray makarov_am@directum.ru merge).
# A plain "GROUP BY login" would pick an arbitrary row (SQLite returns the
# first-encountered one), which could resolve the login to the wrong account and
# make PR/review aggregates miss the developer entirely. Here we deterministically
# choose, per login, the canonical email with the most real (non-merge) commits —
# i.e. the same identity shown in the dashboards.
_LOGIN_MAP = """login_map AS (
                  SELECT login, canonical_email FROM (
                    SELECT
                      LOWER(CASE WHEN INSTR(c.author_email,'@')>0
                                 THEN SUBSTR(c.author_email,1,INSTR(c.author_email,'@')-1)
                                 ELSE c.author_email END) AS login,
                      LOWER(COALESCE(ea.primary_email, c.author_email)) AS canonical_email,
                      ROW_NUMBER() OVER (
                        PARTITION BY LOWER(CASE WHEN INSTR(c.author_email,'@')>0
                                                THEN SUBSTR(c.author_email,1,INSTR(c.author_email,'@')-1)
                                                ELSE c.author_email END)
                        ORDER BY SUM(CASE WHEN c.is_merge=0 THEN 1 ELSE 0 END) DESC,
                                 COUNT(*) DESC,
                                 LOWER(COALESCE(ea.primary_email, c.author_email))
                      ) AS rn
                    FROM commits c
                    LEFT JOIN employee_aliases ea ON c.author_email = ea.alias_email
                    WHERE c.author_email != ''
                    GROUP BY login, canonical_email
                  ) WHERE rn = 1
                )"""

# CTE that resolves each commit's author_email to its canonical (primary) email.
# Use by prepending to any SELECT that groups/filters by author.
_ALIAS_CTE = (
    "WITH cc AS ("
    "  SELECT c.*,"
    "    LOWER(COALESCE(ea.primary_email, c.author_email)) AS canonical_email"
    "  FROM commits c"
    "  LEFT JOIN employee_aliases ea ON c.author_email = ea.alias_email"
    ") "
)

# Condition matching canonical_email (outer) against PR creator_email.
# Handles full email, alias, or bare NT login stored without domain.
_PR_CREATOR_MATCH = (
    "(canonical_email = LOWER(creator_email)"
    " OR canonical_email LIKE LOWER(creator_email) || '@%'"
    " OR canonical_email = (SELECT primary_email FROM employee_aliases"
    "   WHERE alias_email = creator_email)"
    " OR canonical_email = (SELECT primary_email FROM employee_aliases"
    "   WHERE alias_email = (SELECT author_email FROM commits"
    "     WHERE LOWER(author_email) LIKE LOWER(creator_email) || '@%' LIMIT 1)))"
)

# Condition matching canonical_email (outer) against pr_reviews.reviewer_email.
_PR_REVIEWER_MATCH = (
    "(canonical_email = LOWER(rv.reviewer_email)"
    " OR canonical_email LIKE rv.reviewer_email || '@%'"
    " OR canonical_email = (SELECT primary_email FROM employee_aliases"
    "   WHERE alias_email = rv.reviewer_email)"
    " OR canonical_email = (SELECT primary_email FROM employee_aliases"
    "   WHERE alias_email = (SELECT author_email FROM commits"
    "     WHERE LOWER(author_email) LIKE rv.reviewer_email || '@%' LIMIT 1)))"
)

# Scalar expression resolving a PR creator_email to its canonical commit email.
# Matches by login prefix (part before '@') so a full TFS email whose domain
# differs from the commit email still resolves to the same person.
_PR_CANONICAL = (
    "LOWER(COALESCE("
    "  (SELECT primary_email FROM employee_aliases WHERE alias_email = creator_email),"
    "  (SELECT author_email FROM commits"
    "   WHERE LOWER(CASE WHEN INSTR(author_email,'@')>0"
    "                THEN SUBSTR(author_email,1,INSTR(author_email,'@')-1)"
    "                ELSE author_email END)"
    "       = LOWER(CASE WHEN INSTR(creator_email,'@')>0"
    "                THEN SUBSTR(creator_email,1,INSTR(creator_email,'@')-1)"
    "                ELSE creator_email END) LIMIT 1),"
    "  creator_email"
    "))"
)

# Legacy scalar expression for reviewer email resolution (literal context).
_ALIAS_REVIEW_EMAIL = (
    "LOWER(COALESCE("
    "  (SELECT primary_email FROM employee_aliases WHERE alias_email = rv.reviewer_email),"
    "  (SELECT author_email FROM commits"
    "   WHERE LOWER(author_email) LIKE rv.reviewer_email || '@%' LIMIT 1),"
    "  rv.reviewer_email"
    "))"
)


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_tables(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)
        log.info("Database schema ready: %s", self.db_path)

    def clear_data(self):
        with self._conn() as conn:
            conn.executescript("""
                DELETE FROM commits;
                DELETE FROM pr_reviews;
                DELETE FROM pull_requests;
                DELETE FROM sync_log;
                DELETE FROM repositories;
                DELETE FROM projects;
            """)
        log.info("All data cleared from DB")

    # ── settings ────────────────────────────────────────────────────────────

    def save_settings(self, tfs_url: str, collection: str, pat: str):
        with self._conn() as conn:
            for k, v in (("tfs_url", tfs_url), ("collection", collection), ("pat", pat)):
                conn.execute(
                    "INSERT INTO app_settings(key, value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, v),
                )
        log.info("Settings saved (collection=%s)", collection)

    def save_selected_projects(self, project_ids: list[str]):
        import json
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO app_settings(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("selected_projects", json.dumps(project_ids)),
            )
        log.info("Selected projects saved: %s", project_ids)

    def get_selected_projects(self) -> list[str]:
        import json
        s = self.get_settings()
        raw = s.get("selected_projects")
        if not raw:
            return []
        try:
            return json.loads(raw)
        except Exception:
            return []

    def save_selected_employees(self, emails: list[str]):
        import json
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO app_settings(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("selected_employees", json.dumps([e.lower() for e in emails])),
            )
        log.info("Selected employees saved: %d", len(emails))

    def get_selected_employees(self) -> list[str]:
        import json
        s = self.get_settings()
        raw = s.get("selected_employees")
        if not raw:
            return []
        try:
            return json.loads(raw)
        except Exception:
            return []

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _emp_clause(employees: list[str], col: str = "canonical_email") -> tuple[str, list]:
        """Returns extra SQL clause + params for filtering by canonical employee list."""
        if not employees:
            return "", []
        ph = ",".join("?" * len(employees))
        return f" AND {col} IN ({ph})", [e.lower() for e in employees]

    def get_canonical_email(self, email: str) -> str:
        """Return primary_email if this is an alias, otherwise return email as-is."""
        email = email.lower()
        row = self._scalar(
            "SELECT primary_email FROM employee_aliases WHERE alias_email = ?", (email,), default=None
        )
        return row if row else email

    def get_settings(self) -> dict:
        with self._conn() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # ── upsert helpers ───────────────────────────────────────────────────────

    def upsert_project(self, id: str, name: str, collection: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO projects(id, name, collection) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, collection=excluded.collection",
                (id, name, collection),
            )

    def upsert_repository(self, id: str, project_id: str, name: str, default_branch: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO repositories(id, project_id, name, default_branch) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, default_branch=excluded.default_branch",
                (id, project_id, name, default_branch),
            )

    def mark_repo_synced(self, repo_id: str):
        ts = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE repositories SET last_synced=? WHERE id=?", (ts, repo_id)
            )

    def upsert_commit(
        self,
        id: str,
        repo_id: str,
        author_email: str,
        author_name: str,
        author_date: str,
        committer_email: str,
        committer_name: str,
        committer_date: str,
        comment: str,
        changes_add: int,
        changes_edit: int,
        changes_delete: int,
        is_merge: int,
    ):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO commits(
                    id, repo_id, author_email, author_name, author_date,
                    committer_email, committer_name, committer_date,
                    comment, changes_add, changes_edit, changes_delete, is_merge
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    changes_add=excluded.changes_add,
                    changes_edit=excluded.changes_edit,
                    changes_delete=excluded.changes_delete""",
                (
                    id, repo_id,
                    (author_email or "").lower(), author_name,
                    author_date, committer_email, committer_name, committer_date,
                    comment, changes_add, changes_edit, changes_delete, is_merge,
                ),
            )

    def upsert_pull_request(
        self,
        id: int,
        repo_id: str,
        project_id: str,
        title: str,
        creator_email: str,
        creator_name: str,
        status: str,
        created_date: str,
        closed_date: Optional[str],
        target_branch: str,
        source_branch: str,
    ):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO pull_requests(
                    id, repo_id, project_id, title, creator_email, creator_name,
                    status, created_date, closed_date, target_branch, source_branch
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    closed_date=excluded.closed_date,
                    title=excluded.title""",
                (
                    id, repo_id, project_id, title,
                    (creator_email or "").lower(), creator_name,
                    status, created_date, closed_date, target_branch, source_branch,
                ),
            )

    def upsert_pr_review(self, pr_id: int, reviewer_email: str, reviewer_name: str, vote: int):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO pr_reviews(pr_id, reviewer_email, reviewer_name, vote)
                   VALUES(?,?,?,?)
                   ON CONFLICT(pr_id, reviewer_email) DO UPDATE SET
                       vote=excluded.vote,
                       reviewer_name=excluded.reviewer_name""",
                (pr_id, (reviewer_email or "").lower(), reviewer_name, vote),
            )

    def log_sync(self, repo_id: str, sync_type: str, count: int, error: str = None):
        ts = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO sync_log(timestamp, repo_id, type, count, error) VALUES(?,?,?,?,?)",
                (ts, repo_id, sync_type, count, error),
            )

    # ── queries ──────────────────────────────────────────────────────────────

    def _q(self, sql: str, params=()) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def _scalar(self, sql: str, params=(), default=0):
        with self._conn() as conn:
            row = conn.execute(sql, params).fetchone()
        return row[0] if row and row[0] is not None else default

    @staticmethod
    def _qc(conn, sql: str, params=()) -> list[dict]:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    @staticmethod
    def _sc(conn, sql: str, params=(), default=0):
        row = conn.execute(sql, params).fetchone()
        return row[0] if row and row[0] is not None else default

    @staticmethod
    def _pr_logins(emails: list[str]) -> list[str]:
        """Expand canonical emails to include bare NT logins for PR/review table matching."""
        result = []
        for e in emails:
            result.append(e)
            login = e.split('@')[0] if '@' in e else e
            if login != e:
                result.append(login)
        return result

    def get_all_authors(self) -> list[dict]:
        return self._q(
            f"""WITH {_LOGIN_MAP},
                tfs_names AS (
                  SELECT COALESCE(
                           (SELECT lm.canonical_email FROM login_map lm WHERE lm.login = LOWER(
                              CASE WHEN INSTR(p.creator_email,'@')>0
                                   THEN SUBSTR(p.creator_email,1,INSTR(p.creator_email,'@')-1)
                                   ELSE p.creator_email END)),
                           LOWER(p.creator_email)
                         ) AS dev_email,
                         MAX(p.creator_name) AS tfs_name
                  FROM pull_requests p
                  WHERE p.creator_name IS NOT NULL AND p.creator_name != ''
                  GROUP BY dev_email
                  UNION ALL
                  SELECT COALESCE(
                           (SELECT lm.canonical_email FROM login_map lm WHERE lm.login = LOWER(
                              CASE WHEN INSTR(rv.reviewer_email,'@')>0
                                   THEN SUBSTR(rv.reviewer_email,1,INSTR(rv.reviewer_email,'@')-1)
                                   ELSE rv.reviewer_email END)),
                           LOWER(rv.reviewer_email)
                         ) AS dev_email,
                         MAX(rv.reviewer_name) AS tfs_name
                  FROM pr_reviews rv
                  WHERE rv.reviewer_name IS NOT NULL AND rv.reviewer_name != ''
                  GROUP BY dev_email
                ),
                best_names AS (
                  SELECT dev_email, MAX(tfs_name) AS tfs_name FROM tfs_names GROUP BY dev_email
                ),
                cc AS (
                  SELECT c.*,
                    LOWER(COALESCE(ea.primary_email, c.author_email)) AS canonical_email
                  FROM commits c
                  LEFT JOIN employee_aliases ea ON c.author_email = ea.alias_email
                )
            SELECT canonical_email AS author_email,
                   COALESCE(
                     MAX(adn.display_name),
                     MAX(bn.tfs_name),
                     MAX(CASE WHEN author_email = canonical_email THEN author_name END),
                     MAX(author_name)
                   ) AS author_name,
                   COUNT(*) AS commit_count
            FROM cc
            LEFT JOIN best_names bn ON bn.dev_email = canonical_email
            LEFT JOIN author_display_names adn ON adn.email = canonical_email
            WHERE is_merge=0
            GROUP BY canonical_email ORDER BY author_name"""
        )

    def get_overview(self, from_date: str, to_date: str, employees: list[str] = None) -> dict:
        emp_sql, emp_params = self._emp_clause(employees)
        base = f"AND author_date BETWEEN ? AND ? AND is_merge=0{emp_sql}"
        params = [from_date, to_date, *emp_params]
        # PR employee filter: match on the login prefix (part before '@') so a
        # selected employee is found whether their PRs are stored as a bare NT
        # login or as a full email whose domain differs from their commit email.
        pr_logins = [(e.split('@')[0] if '@' in e else e).lower() for e in employees] if employees else []
        if pr_logins:
            ph = ",".join("?" * len(pr_logins))
            pr_emp_sql = (
                " AND LOWER(CASE WHEN INSTR(creator_email,'@')>0"
                " THEN SUBSTR(creator_email,1,INSTR(creator_email,'@')-1)"
                " ELSE creator_email END) IN (" + ph + ")"
            )
        else:
            pr_emp_sql = ""
        with self._conn() as conn:
            # Build login→TFS display name map once for name resolution
            pr_name_rows = self._qc(
                conn,
                "SELECT LOWER(creator_email) AS login, MAX(creator_name) AS tfs_name"
                " FROM pull_requests WHERE creator_name IS NOT NULL AND creator_name != ''"
                " GROUP BY login",
            )
            rv_name_rows = self._qc(
                conn,
                "SELECT LOWER(reviewer_email) AS login, MAX(reviewer_name) AS tfs_name"
                " FROM pr_reviews WHERE reviewer_name IS NOT NULL AND reviewer_name != ''"
                " GROUP BY login",
            )
            name_map: dict[str, str] = {}
            for r in rv_name_rows:
                name_map[r["login"]] = r["tfs_name"]
            for r in pr_name_rows:  # PR names take priority
                name_map[r["login"]] = r["tfs_name"]

            # display_names have highest priority (manual overrides)
            dn_rows = self._qc(conn, "SELECT email, display_name FROM author_display_names")
            display_names: dict[str, str] = {r["email"]: r["display_name"] for r in dn_rows}

            def best_name(dev: dict) -> str:
                email = dev.get("author_email", "")
                login = email.split("@")[0] if "@" in email else email
                return (display_names.get(email) or display_names.get(login)
                        or name_map.get(login) or name_map.get(email)
                        or dev.get("author_name") or email)

            top_contributors = self._qc(
                conn,
                f"""{_ALIAS_CTE}SELECT
                       COALESCE(MAX(CASE WHEN author_email=canonical_email THEN author_name END),
                                MAX(author_name)) AS author_name,
                       canonical_email AS author_email,
                       COUNT(*) AS commit_count,
                       SUM(changes_add+changes_edit+changes_delete) AS total_changes,
                       SUM(changes_add) AS total_add,
                       SUM(changes_edit) AS total_edit,
                       SUM(changes_delete) AS total_delete,
                       COUNT(DISTINCT DATE(author_date)) AS active_days
                    FROM cc WHERE 1=1 {base}
                    GROUP BY canonical_email ORDER BY commit_count DESC LIMIT 10""",
                params,
            )
            for dev in top_contributors:
                dev["author_name"] = best_name(dev)

            recent_commits = self._qc(
                conn,
                f"""{_ALIAS_CTE}SELECT cc.id, cc.author_name, cc.author_email, cc.author_date,
                          cc.comment, cc.changes_add, cc.changes_edit, cc.changes_delete,
                          r.name AS repo_name
                   FROM cc LEFT JOIN repositories r ON cc.repo_id=r.id
                   WHERE cc.is_merge=0{emp_sql}
                   ORDER BY cc.author_date DESC LIMIT 20""",
                emp_params,
            )
            for c in recent_commits:
                c["author_name"] = best_name(c)

            return {
                "total_commits": self._sc(
                    conn, f"{_ALIAS_CTE}SELECT COUNT(*) FROM cc WHERE 1=1 {base}", params
                ),
                "active_devs": self._sc(
                    conn,
                    f"{_ALIAS_CTE}SELECT COUNT(DISTINCT canonical_email) FROM cc WHERE 1=1 {base}",
                    params,
                ),
                "total_repos": self._sc(conn, "SELECT COUNT(*) FROM repositories"),
                "total_prs": self._sc(
                    conn,
                    f"SELECT COUNT(*) FROM pull_requests WHERE created_date BETWEEN ? AND ?{pr_emp_sql}",
                    [from_date, to_date, *pr_logins],
                ),
                "top_contributors": top_contributors,
                "team_heatmap": self._qc(
                    conn,
                    f"""{_ALIAS_CTE}SELECT DATE(author_date) AS day, COUNT(*) AS count
                        FROM cc WHERE 1=1 {base}
                        GROUP BY day ORDER BY day""",
                    params,
                ),
                "recent_commits": recent_commits,
            }

    def get_developers(self, from_date: str, to_date: str, employees: list[str] = None) -> list[dict]:
        emp_sql, emp_params = self._emp_clause(employees)
        # login_map строится один раз: логин (часть email до @) → canonical_email.
        # pr_agg и rv_agg используют его для резолва NT-логинов без LIKE на каждую строку.
        return self._q(
            f"""WITH {_LOGIN_MAP},
                cc AS (
                  SELECT c.*,
                    LOWER(COALESCE(ea.primary_email, c.author_email)) AS canonical_email
                  FROM commits c
                  LEFT JOIN employee_aliases ea ON c.author_email = ea.alias_email
                ),
                pr_agg AS (
                  SELECT
                    COALESCE(
                      (SELECT ea.primary_email FROM employee_aliases ea
                       WHERE ea.alias_email = p.creator_email),
                      (SELECT lm.canonical_email FROM login_map lm
                       WHERE lm.login = LOWER(
                         CASE WHEN INSTR(p.creator_email,'@')>0
                              THEN SUBSTR(p.creator_email,1,INSTR(p.creator_email,'@')-1)
                              ELSE p.creator_email END)),
                      LOWER(p.creator_email)
                    ) AS dev_email,
                    COUNT(*) AS pr_count,
                    MAX(p.creator_name) AS tfs_name
                  FROM pull_requests p
                  WHERE p.created_date BETWEEN ? AND ?
                    AND p.creator_name IS NOT NULL AND p.creator_name != ''
                  GROUP BY dev_email
                ),
                rv_agg AS (
                  SELECT
                    COALESCE(
                      (SELECT ea.primary_email FROM employee_aliases ea
                       WHERE ea.alias_email = rv.reviewer_email),
                      (SELECT lm.canonical_email FROM login_map lm
                       WHERE lm.login = LOWER(
                         CASE WHEN INSTR(rv.reviewer_email,'@')>0
                              THEN SUBSTR(rv.reviewer_email,1,INSTR(rv.reviewer_email,'@')-1)
                              ELSE rv.reviewer_email END)),
                      LOWER(rv.reviewer_email)
                    ) AS dev_email,
                    COUNT(*) AS review_count,
                    SUM(CASE WHEN rv.vote =  10 THEN 1 ELSE 0 END) AS review_approved,
                    SUM(CASE WHEN rv.vote = -10 THEN 1 ELSE 0 END) AS review_rejected,
                    MAX(rv.reviewer_name) AS tfs_name
                  FROM pr_reviews rv
                  JOIN pull_requests pr ON pr.id = rv.pr_id
                  WHERE pr.created_date BETWEEN ? AND ?
                    AND rv.reviewer_name IS NOT NULL AND rv.reviewer_name != ''
                  GROUP BY dev_email
                )
                SELECT cc.canonical_email AS author_email,
                      COALESCE(
                        MAX(adn.display_name),
                        MAX(pr_agg.tfs_name),
                        MAX(rv_agg.tfs_name),
                        MAX(CASE WHEN cc.author_email=cc.canonical_email THEN cc.author_name END),
                        MAX(cc.author_name)
                      ) AS author_name,
                      COUNT(*) AS commit_count,
                      COUNT(DISTINCT DATE(cc.author_date)) AS active_days,
                      SUM(cc.changes_add+cc.changes_edit+cc.changes_delete) AS total_changes,
                      SUM(cc.changes_add) AS total_add,
                      SUM(cc.changes_edit) AS total_edit,
                      SUM(cc.changes_delete) AS total_delete,
                      AVG(cc.changes_add+cc.changes_edit+cc.changes_delete) AS avg_changes,
                      MAX(cc.author_date) AS last_commit,
                      COALESCE(MAX(pr_agg.pr_count), 0) AS pr_count,
                      COALESCE(MAX(rv_agg.review_count), 0) AS review_count,
                      COALESCE(MAX(rv_agg.review_approved), 0) AS review_approved,
                      COALESCE(MAX(rv_agg.review_rejected), 0) AS review_rejected
               FROM cc
               LEFT JOIN pr_agg ON pr_agg.dev_email = cc.canonical_email
               LEFT JOIN rv_agg ON rv_agg.dev_email = cc.canonical_email
               LEFT JOIN author_display_names adn ON adn.email = cc.canonical_email
               WHERE cc.author_date BETWEEN ? AND ? AND cc.is_merge=0{emp_sql}
               GROUP BY cc.canonical_email
               ORDER BY commit_count DESC""",
            [from_date, to_date,   # pr_agg
             from_date, to_date,   # rv_agg
             from_date, to_date,   # main WHERE
             *emp_params],
        )

    def get_developer_stats(self, email: str, from_date: str, to_date: str) -> dict:
        email = email.lower()
        # Bare NT login is the part before '@'; PRs/reviews may store the bare
        # login OR a full email whose domain differs from the commit email
        # (e.g. commits as makarov_am@w1458w10 but PRs as makarov_am@directum.ru).
        # Match on the login prefix so all such forms resolve to the same person.
        login = email.split('@')[0] if '@' in email else email
        pr_ids = (email, login, from_date, to_date)
        p = (email, from_date, to_date)
        # (login, from, to) — used by PR/review queries that match on login prefix
        login_pr = (login, from_date, to_date)
        with self._conn() as conn:
            return {
                "summary": self._qc(
                    conn,
                    f"""{_ALIAS_CTE}SELECT COUNT(*) AS commit_count,
                              COUNT(DISTINCT DATE(author_date)) AS active_days,
                              SUM(changes_add) AS total_add,
                              SUM(changes_edit) AS total_edit,
                              SUM(changes_delete) AS total_delete,
                              AVG(changes_add+changes_edit+changes_delete) AS avg_changes,
                              MAX(author_date) AS last_commit
                       FROM cc
                       WHERE canonical_email=? AND author_date BETWEEN ? AND ? AND is_merge=0""",
                    p,
                ),
                "heatmap": self._qc(
                    conn,
                    f"""{_ALIAS_CTE}SELECT DATE(author_date) AS day, COUNT(*) AS count
                       FROM cc
                       WHERE canonical_email=? AND author_date BETWEEN ? AND ? AND is_merge=0
                       GROUP BY day ORDER BY day""",
                    p,
                ),
                "daily": self._qc(
                    conn,
                    f"""{_ALIAS_CTE}SELECT DATE(author_date) AS day,
                              COUNT(*) AS commits,
                              SUM(changes_add) AS adds,
                              SUM(changes_edit) AS edits,
                              SUM(changes_delete) AS deletes
                       FROM cc
                       WHERE canonical_email=? AND author_date BETWEEN ? AND ? AND is_merge=0
                       GROUP BY day ORDER BY day""",
                    p,
                ),
                "pr_stats": self._qc(
                    conn,
                    """SELECT status, COUNT(*) AS count
                       FROM pull_requests
                       WHERE LOWER(CASE WHEN INSTR(creator_email,'@')>0
                                        THEN SUBSTR(creator_email,1,INSTR(creator_email,'@')-1)
                                        ELSE creator_email END) = ?
                         AND created_date BETWEEN ? AND ?
                       GROUP BY status""",
                    login_pr,
                ),
                "repos": self._qc(
                    conn,
                    f"""{_ALIAS_CTE}SELECT r.name AS repo_name, r.id AS repo_id,
                              COUNT(*) AS commit_count,
                              SUM(cc.changes_add+cc.changes_edit+cc.changes_delete) AS total_changes
                       FROM cc LEFT JOIN repositories r ON cc.repo_id=r.id
                       WHERE cc.canonical_email=? AND cc.author_date BETWEEN ? AND ? AND cc.is_merge=0
                       GROUP BY cc.repo_id ORDER BY commit_count DESC""",
                    p,
                ),
                "info": self._qc(
                    conn,
                    f"""{_ALIAS_CTE}SELECT canonical_email AS author_email,
                           COALESCE(
                             (SELECT display_name FROM author_display_names
                              WHERE email = canonical_email LIMIT 1),
                             (SELECT p.creator_name FROM pull_requests p
                              WHERE LOWER(CASE WHEN INSTR(p.creator_email,'@')>0
                                               THEN SUBSTR(p.creator_email,1,INSTR(p.creator_email,'@')-1)
                                               ELSE p.creator_email END) = ?
                                AND p.creator_name IS NOT NULL AND p.creator_name != ''
                              LIMIT 1),
                             (SELECT rv.reviewer_name FROM pr_reviews rv
                              WHERE LOWER(CASE WHEN INSTR(rv.reviewer_email,'@')>0
                                               THEN SUBSTR(rv.reviewer_email,1,INSTR(rv.reviewer_email,'@')-1)
                                               ELSE rv.reviewer_email END) = ?
                                AND rv.reviewer_name IS NOT NULL AND rv.reviewer_name != ''
                              LIMIT 1),
                             MAX(CASE WHEN author_email=canonical_email THEN author_name END),
                             MAX(author_name)
                           ) AS author_name
                       FROM cc WHERE canonical_email=?""",
                    (login, login, email),
                ),
                "active_weeks": self._sc(
                    conn,
                    f"""{_ALIAS_CTE}SELECT COUNT(DISTINCT strftime('%Y-%W', author_date))
                       FROM cc WHERE canonical_email=? AND author_date BETWEEN ? AND ? AND is_merge=0""",
                    (email, from_date, to_date),
                ),
                "aliases": self._qc(
                    conn,
                    "SELECT alias_email FROM employee_aliases WHERE primary_email=?",
                    (email,),
                ),
                "review_stats": self._qc(
                    conn,
                    """SELECT
                           COUNT(*) AS reviews_total,
                           SUM(CASE WHEN vote=10 THEN 1 ELSE 0 END) AS approved
                       FROM pr_reviews rv
                       JOIN pull_requests pr ON pr.id=rv.pr_id
                       WHERE LOWER(CASE WHEN INSTR(rv.reviewer_email,'@')>0
                                        THEN SUBSTR(rv.reviewer_email,1,INSTR(rv.reviewer_email,'@')-1)
                                        ELSE rv.reviewer_email END) = ?
                         AND pr.created_date BETWEEN ? AND ?""",
                    login_pr,
                ),
            }

    def get_compare_stats(self, emails: list[str], from_date: str, to_date: str) -> list[dict]:
        placeholders = ",".join("?" * len(emails))
        emails_lower = [e.lower() for e in emails]
        rows = self._q(
            f"""{_ALIAS_CTE}SELECT canonical_email AS author_email,
                       COALESCE(MAX(CASE WHEN author_email=canonical_email THEN author_name END),
                                MAX(author_name)) AS author_name,
                       COUNT(*) AS commit_count,
                       COUNT(DISTINCT DATE(author_date)) AS active_days,
                       AVG(changes_add+changes_edit+changes_delete) AS avg_changes,
                       COUNT(DISTINCT repo_id) AS repos_touched
                FROM cc
                WHERE canonical_email IN ({placeholders})
                  AND author_date BETWEEN ? AND ? AND is_merge=0
                GROUP BY canonical_email""",
            [*emails_lower, from_date, to_date],
        )
        pr_rows = self._q(
            f"""SELECT {_PR_CANONICAL} AS canonical_email,
                       COUNT(*) AS pr_count,
                       SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS pr_merged
                FROM pull_requests
                WHERE {_PR_CANONICAL} IN ({placeholders})
                  AND created_date BETWEEN ? AND ?
                GROUP BY canonical_email""",
            [*emails_lower, from_date, to_date],
        )
        pr_map = {r["canonical_email"]: r for r in pr_rows}

        timeline = self._q(
            f"""{_ALIAS_CTE}SELECT canonical_email AS author_email,
                       strftime('%Y-%W', author_date) AS week,
                       COUNT(*) AS commits
                FROM cc
                WHERE canonical_email IN ({placeholders})
                  AND author_date BETWEEN ? AND ? AND is_merge=0
                GROUP BY canonical_email, week ORDER BY week""",
            [*emails_lower, from_date, to_date],
        )

        for r in rows:
            pr = pr_map.get(r["author_email"], {})
            r["pr_count"] = pr.get("pr_count", 0)
            r["pr_merged"] = pr.get("pr_merged", 0)
            r["pr_merge_rate"] = round(r["pr_merged"] / r["pr_count"] * 100, 1) if r.get("pr_count") else 0

        return {"developers": rows, "timeline": timeline}

    def get_repositories(self, from_date: str, to_date: str, employees: list[str] = None) -> list[dict]:
        emp_sql, emp_params = self._emp_clause(employees, col="cc.canonical_email")
        having = "HAVING commit_count > 0" if employees else ""
        return self._q(
            f"""{_ALIAS_CTE}SELECT r.id, r.name, r.project_id, COALESCE(p.name, r.project_id) AS project_name, r.last_synced,
                      COUNT(DISTINCT cc.id) AS commit_count,
                      COUNT(DISTINCT cc.canonical_email) AS author_count,
                      MAX(cc.author_date) AS last_commit
               FROM repositories r
               LEFT JOIN projects p ON p.id=r.project_id
               LEFT JOIN cc ON r.id=cc.repo_id
                 AND cc.author_date BETWEEN ? AND ? AND cc.is_merge=0{emp_sql}
               GROUP BY r.id {having} ORDER BY commit_count DESC""",
            [from_date, to_date, *emp_params],
        )

    def get_repository_stats(self, repo_id: str, from_date: str, to_date: str, employees: list[str] = None) -> dict:
        emp_sql, emp_params = self._emp_clause(employees, col="cc.canonical_email")
        p = [repo_id, from_date, to_date, *emp_params]
        return {
            "info": self._q("SELECT * FROM repositories WHERE id=?", (repo_id,)),
            "top_authors": self._q(
                f"""{_ALIAS_CTE}SELECT
                       COALESCE(MAX(CASE WHEN author_email=canonical_email THEN author_name END),
                                MAX(author_name)) AS author_name,
                       canonical_email AS author_email,
                       COUNT(*) AS commit_count
                   FROM cc
                   WHERE repo_id=? AND author_date BETWEEN ? AND ? AND is_merge=0{emp_sql}
                   GROUP BY canonical_email ORDER BY commit_count DESC LIMIT 10""",
                p,
            ),
            "heatmap": self._q(
                f"""{_ALIAS_CTE}SELECT DATE(author_date) AS day, COUNT(*) AS count
                   FROM cc
                   WHERE repo_id=? AND author_date BETWEEN ? AND ? AND is_merge=0{emp_sql}
                   GROUP BY day ORDER BY day""",
                p,
            ),
        }

    # ── alias management ────────────────────────────────────────────────────────

    def get_alias_groups(self) -> list[dict]:
        rows = self._q(
            """SELECT ea.primary_email,
                      COALESCE((SELECT author_name FROM commits WHERE author_email=ea.primary_email
                                ORDER BY author_date DESC LIMIT 1), ea.primary_email) AS primary_name,
                      ea.alias_email,
                      COALESCE((SELECT author_name FROM commits WHERE author_email=ea.alias_email
                                ORDER BY author_date DESC LIMIT 1), ea.alias_email) AS alias_name
               FROM employee_aliases ea
               ORDER BY ea.primary_email, ea.alias_email"""
        )
        groups: dict = {}
        for r in rows:
            pe = r["primary_email"]
            if pe not in groups:
                groups[pe] = {"primary_email": pe, "primary_name": r["primary_name"], "aliases": []}
            groups[pe]["aliases"].append({"email": r["alias_email"], "name": r["alias_name"]})
        return list(groups.values())

    def add_alias(self, primary_email: str, alias_email: str) -> str | None:
        """Add alias → primary mapping. Returns error string or None on success."""
        primary_email = primary_email.lower().strip()
        alias_email   = alias_email.lower().strip()
        if primary_email == alias_email:
            return "Нельзя объединить аккаунт с самим собой"
        with self._conn() as conn:
            # alias_email must not already be a primary_email
            row = conn.execute(
                "SELECT 1 FROM employee_aliases WHERE primary_email=?", (alias_email,)
            ).fetchone()
            if row:
                return f"{alias_email} уже является основным аккаунтом — нельзя сделать его псевдонимом"
            # primary_email must not already be an alias
            row = conn.execute(
                "SELECT primary_email FROM employee_aliases WHERE alias_email=?", (primary_email,)
            ).fetchone()
            if row:
                return f"{primary_email} уже является псевдонимом {row[0]}"
            conn.execute(
                "INSERT INTO employee_aliases(primary_email, alias_email) VALUES(?,?)"
                " ON CONFLICT(alias_email) DO UPDATE SET primary_email=excluded.primary_email",
                (primary_email, alias_email),
            )
        log.info("Alias added: %s → %s", alias_email, primary_email)
        return None

    def remove_alias(self, alias_email: str):
        with self._conn() as conn:
            conn.execute("DELETE FROM employee_aliases WHERE alias_email=?", (alias_email.lower(),))
        log.info("Alias removed: %s", alias_email)

    def remove_alias_group(self, primary_email: str):
        with self._conn() as conn:
            conn.execute("DELETE FROM employee_aliases WHERE primary_email=?", (primary_email.lower(),))
        log.info("Alias group removed: primary=%s", primary_email)

    # ── teams ────────────────────────────────────────────────────────────────────

    def get_teams(self) -> list[dict]:
        """Return all teams with member emails."""
        teams = self._q("SELECT id, name FROM teams ORDER BY name")
        members_all = self._q("SELECT team_id, email FROM team_members ORDER BY email")
        by_team: dict[int, list[str]] = {}
        for m in members_all:
            by_team.setdefault(m["team_id"], []).append(m["email"])
        for t in teams:
            t["members"] = by_team.get(t["id"], [])
        return teams

    def create_team(self, name: str) -> int | None:
        """Create team; return new id or None if name already exists."""
        name = name.strip()
        if not name:
            return None
        try:
            with self._conn() as conn:
                cur = conn.execute("INSERT INTO teams(name) VALUES(?)", (name,))
                return cur.lastrowid
        except Exception:
            return None

    def rename_team(self, team_id: int, name: str) -> bool:
        name = name.strip()
        if not name:
            return False
        try:
            with self._conn() as conn:
                conn.execute("UPDATE teams SET name=? WHERE id=?", (name, team_id))
            return True
        except Exception:
            return False

    def delete_team(self, team_id: int):
        with self._conn() as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("DELETE FROM teams WHERE id=?", (team_id,))
        # Clear selected team if it was this one
        if self.get_selected_team() == team_id:
            self.save_selected_team(0)

    def add_team_member(self, team_id: int, email: str) -> bool:
        email = email.lower().strip()
        if not email:
            return False
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO team_members(team_id, email) VALUES(?,?)",
                    (team_id, email),
                )
            return True
        except Exception:
            return False

    def remove_team_member(self, team_id: int, email: str):
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM team_members WHERE team_id=? AND email=?",
                (team_id, email.lower()),
            )

    def get_team_members(self, team_id: int) -> list[str]:
        rows = self._q(
            "SELECT email FROM team_members WHERE team_id=? ORDER BY email", (team_id,)
        )
        return [r["email"] for r in rows]

    def get_selected_team(self) -> int:
        val = self._scalar(
            "SELECT value FROM app_settings WHERE key='selected_team_id'", default=None
        )
        try:
            return int(val) if val else 0
        except (TypeError, ValueError):
            return 0

    def save_selected_team(self, team_id: int):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO app_settings(key, value) VALUES('selected_team_id',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(team_id),),
            )
        log.info("Selected team: %s", team_id)

    def get_all_emails(self) -> list[dict]:
        """All unique canonical emails for alias/display-name forms.
        Includes commit authors AND people who only appear in PRs or reviews.
        Returns [{author_email, author_name, source}] sorted by name."""
        return self._q(
            f"""WITH {_LOGIN_MAP},
                from_commits AS (
                  SELECT LOWER(COALESCE(ea.primary_email, c.author_email)) AS email,
                         COALESCE(MAX(adn.display_name),
                                  MAX(CASE WHEN c.author_email=LOWER(COALESCE(ea.primary_email, c.author_email))
                                           THEN c.author_name END),
                                  MAX(c.author_name)) AS name,
                         'commit' AS src
                  FROM commits c
                  LEFT JOIN employee_aliases ea ON c.author_email = ea.alias_email
                  LEFT JOIN author_display_names adn
                         ON adn.email = LOWER(COALESCE(ea.primary_email, c.author_email))
                  WHERE c.author_email != ''
                  -- group by the full expression, NOT the alias "email": the
                  -- joined author_display_names.email column shadows the alias and
                  -- would otherwise collapse every commit author into one NULL group.
                  GROUP BY LOWER(COALESCE(ea.primary_email, c.author_email))
                ),
                from_prs AS (
                  SELECT COALESCE(
                           (SELECT lm.canonical_email FROM login_map lm WHERE lm.login=LOWER(
                              CASE WHEN INSTR(p.creator_email,'@')>0
                                   THEN SUBSTR(p.creator_email,1,INSTR(p.creator_email,'@')-1)
                                   ELSE p.creator_email END)),
                           LOWER(p.creator_email)
                         ) AS email,
                         MAX(p.creator_name) AS name,
                         'pr' AS src
                  FROM pull_requests p
                  WHERE p.creator_email != '' AND p.creator_name IS NOT NULL AND p.creator_name != ''
                  GROUP BY email
                ),
                from_reviews AS (
                  SELECT COALESCE(
                           (SELECT lm.canonical_email FROM login_map lm WHERE lm.login=LOWER(
                              CASE WHEN INSTR(rv.reviewer_email,'@')>0
                                   THEN SUBSTR(rv.reviewer_email,1,INSTR(rv.reviewer_email,'@')-1)
                                   ELSE rv.reviewer_email END)),
                           LOWER(rv.reviewer_email)
                         ) AS email,
                         MAX(rv.reviewer_name) AS name,
                         'review' AS src
                  FROM pr_reviews rv
                  WHERE rv.reviewer_email != '' AND rv.reviewer_name IS NOT NULL AND rv.reviewer_name != ''
                  GROUP BY email
                ),
                combined AS (
                  SELECT email, name, src FROM from_commits
                  UNION SELECT email, name, src FROM from_prs
                  UNION SELECT email, name, src FROM from_reviews
                )
            SELECT email AS author_email,
                   COALESCE(MAX(CASE WHEN src='pr' THEN name END),
                            MAX(CASE WHEN src='review' THEN name END),
                            MAX(name)) AS author_name,
                   MAX(src) AS source
            FROM combined
            GROUP BY email
            ORDER BY author_name"""
        )

    # ── display name overrides ───────────────────────────────────────────────────

    def get_display_names(self) -> dict[str, str]:
        """Return {email: display_name} for all manual overrides."""
        rows = self._q("SELECT email, display_name FROM author_display_names ORDER BY email")
        return {r["email"]: r["display_name"] for r in rows}

    def set_display_name(self, email: str, display_name: str):
        """Upsert a manual display name override for an email."""
        email = email.lower().strip()
        display_name = display_name.strip()
        if not email or not display_name:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO author_display_names(email, display_name) VALUES(?,?)"
                " ON CONFLICT(email) DO UPDATE SET display_name=excluded.display_name",
                (email, display_name),
            )
        log.info("Display name set: %s → %s", email, display_name)

    def delete_display_name(self, email: str):
        """Remove a manual display name override."""
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM author_display_names WHERE email=?", (email.lower().strip(),)
            )
        log.info("Display name deleted: %s", email)

    def get_sync_log(self, limit: int = 50) -> list[dict]:
        return self._q(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,)
        )

    def get_cached_range(self) -> dict:
        row = self._q(
            "SELECT MIN(author_date) as min_date, MAX(author_date) as max_date FROM commits"
        )
        return row[0] if row else {"min_date": None, "max_date": None}
