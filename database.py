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

CREATE INDEX IF NOT EXISTS idx_commits_author_date  ON commits(author_email, author_date);
CREATE INDEX IF NOT EXISTS idx_commits_repo_date    ON commits(repo_id, author_date);
CREATE INDEX IF NOT EXISTS idx_commits_date         ON commits(author_date);
CREATE INDEX IF NOT EXISTS idx_pr_creator_date      ON pull_requests(creator_email, created_date);
CREATE INDEX IF NOT EXISTS idx_pr_repo              ON pull_requests(repo_id);
CREATE INDEX IF NOT EXISTS idx_aliases_primary      ON employee_aliases(primary_email);
"""

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

# Inline expression to resolve PR creator email to canonical email.
_PR_CANONICAL = (
    "LOWER(COALESCE("
    "  (SELECT primary_email FROM employee_aliases WHERE alias_email = creator_email),"
    "  creator_email"
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

    def get_all_authors(self) -> list[dict]:
        return self._q(
            f"""{_ALIAS_CTE}
            SELECT canonical_email AS author_email,
                   COALESCE(MAX(CASE WHEN author_email = canonical_email THEN author_name END),
                            MAX(author_name)) AS author_name,
                   COUNT(*) AS commit_count
            FROM cc WHERE is_merge=0
            GROUP BY canonical_email ORDER BY author_name"""
        )

    def get_overview(self, from_date: str, to_date: str, employees: list[str] = None) -> dict:
        emp_sql, emp_params = self._emp_clause(employees)
        base = f"AND author_date BETWEEN ? AND ? AND is_merge=0{emp_sql}"
        params = [from_date, to_date, *emp_params]
        pr_emp_sql = ""
        if employees:
            ph = ",".join("?" * len(employees))
            pr_emp_sql = f" AND {_PR_CANONICAL} IN ({ph})"
        return {
            "total_commits": self._scalar(
                f"{_ALIAS_CTE}SELECT COUNT(*) FROM cc WHERE 1=1 {base}", params
            ),
            "active_devs": self._scalar(
                f"{_ALIAS_CTE}SELECT COUNT(DISTINCT canonical_email) FROM cc WHERE 1=1 {base}", params
            ),
            "total_repos": self._scalar("SELECT COUNT(*) FROM repositories"),
            "total_prs": self._scalar(
                f"SELECT COUNT(*) FROM pull_requests WHERE created_date BETWEEN ? AND ?{pr_emp_sql}",
                [from_date, to_date, *emp_params],
            ),
            "top_contributors": self._q(
                f"""{_ALIAS_CTE}SELECT
                       COALESCE(MAX(CASE WHEN author_email=canonical_email THEN author_name END),
                                MAX(author_name)) AS author_name,
                       canonical_email AS author_email,
                       COUNT(*) AS commit_count,
                       SUM(changes_add+changes_edit+changes_delete) AS total_changes
                    FROM cc WHERE 1=1 {base}
                    GROUP BY canonical_email ORDER BY commit_count DESC LIMIT 10""",
                params,
            ),
            "team_heatmap": self._q(
                f"""{_ALIAS_CTE}SELECT DATE(author_date) AS day, COUNT(*) AS count
                    FROM cc WHERE 1=1 {base}
                    GROUP BY day ORDER BY day""",
                params,
            ),
            "recent_commits": self._q(
                f"""{_ALIAS_CTE}SELECT cc.id, cc.author_name, cc.author_email, cc.author_date,
                          cc.comment, cc.changes_add, cc.changes_edit, cc.changes_delete,
                          r.name AS repo_name
                   FROM cc LEFT JOIN repositories r ON cc.repo_id=r.id
                   WHERE cc.is_merge=0{emp_sql}
                   ORDER BY cc.author_date DESC LIMIT 20""",
                emp_params,
            ),
        }

    def get_developers(self, from_date: str, to_date: str, employees: list[str] = None) -> list[dict]:
        emp_sql, emp_params = self._emp_clause(employees)
        return self._q(
            f"""{_ALIAS_CTE}SELECT canonical_email AS author_email,
                      COALESCE(MAX(CASE WHEN author_email=canonical_email THEN author_name END),
                               MAX(author_name)) AS author_name,
                      COUNT(*) AS commit_count,
                      COUNT(DISTINCT DATE(author_date)) AS active_days,
                      SUM(changes_add+changes_edit+changes_delete) AS total_changes,
                      MAX(author_date) AS last_commit
               FROM cc
               WHERE author_date BETWEEN ? AND ? AND is_merge=0{emp_sql}
               GROUP BY canonical_email
               ORDER BY commit_count DESC""",
            [from_date, to_date, *emp_params],
        )

    def get_developer_stats(self, email: str, from_date: str, to_date: str) -> dict:
        email = email.lower()
        p = (email, from_date, to_date)
        return {
            "summary": self._q(
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
            "heatmap": self._q(
                f"""{_ALIAS_CTE}SELECT DATE(author_date) AS day, COUNT(*) AS count
                   FROM cc
                   WHERE canonical_email=? AND author_date BETWEEN ? AND ? AND is_merge=0
                   GROUP BY day ORDER BY day""",
                p,
            ),
            "daily": self._q(
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
            "pr_stats": self._q(
                f"""SELECT status, COUNT(*) AS count
                   FROM pull_requests
                   WHERE {_PR_CANONICAL}=? AND created_date BETWEEN ? AND ?
                   GROUP BY status""",
                p,
            ),
            "repos": self._q(
                f"""{_ALIAS_CTE}SELECT r.name AS repo_name, r.id AS repo_id,
                          COUNT(*) AS commit_count,
                          SUM(cc.changes_add+cc.changes_edit+cc.changes_delete) AS total_changes
                   FROM cc LEFT JOIN repositories r ON cc.repo_id=r.id
                   WHERE cc.canonical_email=? AND cc.author_date BETWEEN ? AND ? AND cc.is_merge=0
                   GROUP BY cc.repo_id ORDER BY commit_count DESC""",
                p,
            ),
            "info": self._q(
                f"""{_ALIAS_CTE}SELECT canonical_email AS author_email,
                       COALESCE(MAX(CASE WHEN author_email=canonical_email THEN author_name END),
                                MAX(author_name)) AS author_name
                   FROM cc WHERE canonical_email=?""",
                (email,),
            ),
            "aliases": self._q(
                """SELECT alias_email FROM employee_aliases WHERE primary_email=?""",
                (email,),
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
        return self._q(
            f"""{_ALIAS_CTE}SELECT r.id, r.name, r.project_id, r.last_synced,
                      COUNT(DISTINCT cc.id) AS commit_count,
                      COUNT(DISTINCT cc.canonical_email) AS author_count,
                      MAX(cc.author_date) AS last_commit
               FROM repositories r
               LEFT JOIN cc ON r.id=cc.repo_id
                 AND cc.author_date BETWEEN ? AND ? AND cc.is_merge=0{emp_sql}
               GROUP BY r.id ORDER BY commit_count DESC""",
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

    def get_sync_log(self, limit: int = 50) -> list[dict]:
        return self._q(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,)
        )

    def get_cached_range(self) -> dict:
        row = self._q(
            "SELECT MIN(author_date) as min_date, MAX(author_date) as max_date FROM commits"
        )
        return row[0] if row else {"min_date": None, "max_date": None}
