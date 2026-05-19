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

CREATE INDEX IF NOT EXISTS idx_commits_author_date  ON commits(author_email, author_date);
CREATE INDEX IF NOT EXISTS idx_commits_repo_date    ON commits(repo_id, author_date);
CREATE INDEX IF NOT EXISTS idx_commits_date         ON commits(author_date);
CREATE INDEX IF NOT EXISTS idx_pr_creator_date      ON pull_requests(creator_email, created_date);
CREATE INDEX IF NOT EXISTS idx_pr_repo              ON pull_requests(repo_id);
"""


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

    def get_overview(self, from_date: str, to_date: str) -> dict:
        base = "AND author_date BETWEEN ? AND ? AND is_merge=0"
        params = (from_date, to_date)
        return {
            "total_commits": self._scalar(
                f"SELECT COUNT(*) FROM commits WHERE 1=1 {base}", params
            ),
            "active_devs": self._scalar(
                f"SELECT COUNT(DISTINCT author_email) FROM commits WHERE 1=1 {base}", params
            ),
            "total_repos": self._scalar("SELECT COUNT(*) FROM repositories"),
            "total_prs": self._scalar(
                "SELECT COUNT(*) FROM pull_requests WHERE created_date BETWEEN ? AND ?", params
            ),
            "top_contributors": self._q(
                f"""SELECT author_name, author_email, COUNT(*) as commit_count,
                           SUM(changes_add+changes_edit+changes_delete) as total_changes
                    FROM commits WHERE 1=1 {base}
                    GROUP BY author_email ORDER BY commit_count DESC LIMIT 10""",
                params,
            ),
            "team_heatmap": self._q(
                f"""SELECT DATE(author_date) as day, COUNT(*) as count
                    FROM commits WHERE 1=1 {base}
                    GROUP BY day ORDER BY day""",
                params,
            ),
            "recent_commits": self._q(
                """SELECT c.id, c.author_name, c.author_email, c.author_date,
                          c.comment, c.changes_add, c.changes_edit, c.changes_delete,
                          r.name as repo_name
                   FROM commits c LEFT JOIN repositories r ON c.repo_id=r.id
                   WHERE c.is_merge=0
                   ORDER BY c.author_date DESC LIMIT 20"""
            ),
        }

    def get_developers(self, from_date: str, to_date: str) -> list[dict]:
        return self._q(
            """SELECT author_email, author_name,
                      COUNT(*) as commit_count,
                      COUNT(DISTINCT DATE(author_date)) as active_days,
                      SUM(changes_add+changes_edit+changes_delete) as total_changes,
                      MAX(author_date) as last_commit
               FROM commits
               WHERE author_date BETWEEN ? AND ? AND is_merge=0
               GROUP BY author_email
               ORDER BY commit_count DESC""",
            (from_date, to_date),
        )

    def get_developer_stats(self, email: str, from_date: str, to_date: str) -> dict:
        email = email.lower()
        p = (email, from_date, to_date)
        return {
            "summary": self._q(
                """SELECT COUNT(*) as commit_count,
                          COUNT(DISTINCT DATE(author_date)) as active_days,
                          SUM(changes_add) as total_add,
                          SUM(changes_edit) as total_edit,
                          SUM(changes_delete) as total_delete,
                          AVG(changes_add+changes_edit+changes_delete) as avg_changes,
                          MAX(author_date) as last_commit
                   FROM commits
                   WHERE author_email=? AND author_date BETWEEN ? AND ? AND is_merge=0""",
                p,
            ),
            "heatmap": self._q(
                """SELECT DATE(author_date) as day, COUNT(*) as count
                   FROM commits
                   WHERE author_email=? AND author_date BETWEEN ? AND ? AND is_merge=0
                   GROUP BY day ORDER BY day""",
                p,
            ),
            "daily": self._q(
                """SELECT DATE(author_date) as day,
                          COUNT(*) as commits,
                          SUM(changes_add) as adds,
                          SUM(changes_edit) as edits,
                          SUM(changes_delete) as deletes
                   FROM commits
                   WHERE author_email=? AND author_date BETWEEN ? AND ? AND is_merge=0
                   GROUP BY day ORDER BY day""",
                p,
            ),
            "pr_stats": self._q(
                """SELECT status, COUNT(*) as count
                   FROM pull_requests
                   WHERE creator_email=? AND created_date BETWEEN ? AND ?
                   GROUP BY status""",
                p,
            ),
            "repos": self._q(
                """SELECT r.name as repo_name, r.id as repo_id, COUNT(*) as commit_count,
                          SUM(c.changes_add+c.changes_edit+c.changes_delete) as total_changes
                   FROM commits c LEFT JOIN repositories r ON c.repo_id=r.id
                   WHERE c.author_email=? AND c.author_date BETWEEN ? AND ? AND c.is_merge=0
                   GROUP BY c.repo_id ORDER BY commit_count DESC""",
                p,
            ),
            "info": self._q(
                """SELECT author_name, author_email FROM commits
                   WHERE author_email=? LIMIT 1""",
                (email,),
            ),
        }

    def get_compare_stats(self, emails: list[str], from_date: str, to_date: str) -> list[dict]:
        placeholders = ",".join("?" * len(emails))
        emails_lower = [e.lower() for e in emails]
        rows = self._q(
            f"""SELECT author_email, author_name,
                       COUNT(*) as commit_count,
                       COUNT(DISTINCT DATE(author_date)) as active_days,
                       AVG(changes_add+changes_edit+changes_delete) as avg_changes,
                       COUNT(DISTINCT repo_id) as repos_touched
                FROM commits
                WHERE author_email IN ({placeholders})
                  AND author_date BETWEEN ? AND ? AND is_merge=0
                GROUP BY author_email""",
            [*emails_lower, from_date, to_date],
        )
        pr_rows = self._q(
            f"""SELECT creator_email,
                       COUNT(*) as pr_count,
                       SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as pr_merged
                FROM pull_requests
                WHERE creator_email IN ({placeholders})
                  AND created_date BETWEEN ? AND ?
                GROUP BY creator_email""",
            [*emails_lower, from_date, to_date],
        )
        pr_map = {r["creator_email"]: r for r in pr_rows}

        timeline = self._q(
            f"""SELECT author_email,
                       strftime('%Y-%W', author_date) as week,
                       COUNT(*) as commits
                FROM commits
                WHERE author_email IN ({placeholders})
                  AND author_date BETWEEN ? AND ? AND is_merge=0
                GROUP BY author_email, week ORDER BY week""",
            [*emails_lower, from_date, to_date],
        )

        for r in rows:
            pr = pr_map.get(r["author_email"], {})
            r["pr_count"] = pr.get("pr_count", 0)
            r["pr_merged"] = pr.get("pr_merged", 0)
            r["pr_merge_rate"] = round(r["pr_merged"] / r["pr_count"] * 100, 1) if r.get("pr_count") else 0

        return {"developers": rows, "timeline": timeline}

    def get_repositories(self, from_date: str, to_date: str) -> list[dict]:
        return self._q(
            """SELECT r.id, r.name, r.project_id, r.last_synced,
                      COUNT(DISTINCT c.id) as commit_count,
                      COUNT(DISTINCT c.author_email) as author_count,
                      MAX(c.author_date) as last_commit
               FROM repositories r
               LEFT JOIN commits c ON r.id=c.repo_id
                 AND c.author_date BETWEEN ? AND ? AND c.is_merge=0
               GROUP BY r.id ORDER BY commit_count DESC""",
            (from_date, to_date),
        )

    def get_repository_stats(self, repo_id: str, from_date: str, to_date: str) -> dict:
        p = (repo_id, from_date, to_date)
        return {
            "info": self._q("SELECT * FROM repositories WHERE id=?", (repo_id,)),
            "top_authors": self._q(
                """SELECT author_name, author_email, COUNT(*) as commit_count
                   FROM commits
                   WHERE repo_id=? AND author_date BETWEEN ? AND ? AND is_merge=0
                   GROUP BY author_email ORDER BY commit_count DESC LIMIT 10""",
                p,
            ),
            "heatmap": self._q(
                """SELECT DATE(author_date) as day, COUNT(*) as count
                   FROM commits
                   WHERE repo_id=? AND author_date BETWEEN ? AND ? AND is_merge=0
                   GROUP BY day ORDER BY day""",
                p,
            ),
        }

    def get_sync_log(self, limit: int = 50) -> list[dict]:
        return self._q(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,)
        )

    def get_cached_range(self) -> dict:
        row = self._q(
            "SELECT MIN(author_date) as min_date, MAX(author_date) as max_date FROM commits"
        )
        return row[0] if row else {"min_date": None, "max_date": None}
