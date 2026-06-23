import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, jsonify, render_template, request

from collector import TFSClient
from config import Config
from database import Database
from logger import setup_logger
from scheduler import get_sync_status, start_scheduler, start_sync_async, stop_scheduler

cfg = Config()
log = setup_logger("app", log_file=cfg.log_file, level=cfg.log_level)
setup_logger("collector", log_file=cfg.log_file, level=cfg.log_level)
setup_logger("database", log_file=cfg.log_file, level=cfg.log_level)
setup_logger("scheduler", log_file=cfg.log_file, level=cfg.log_level)

db = Database(cfg.database_path)
db.create_tables()

app = Flask(__name__)

# ── simple in-memory cache ────────────────────────────────────────────────────
_cache: dict = {}
_CACHE_TTL = 60  # seconds


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < _CACHE_TTL:
        return entry["data"]
    return None


def _cache_set(key: str, data):
    _cache[key] = {"ts": time.time(), "data": data}


def _cache_clear():
    _cache.clear()


# ── helpers ──────────────────────────────────────────────────────────────────

def _ok(data):
    return jsonify({"success": True, "data": data, "error": None})


def _err(msg: str, status: int = 400):
    log.warning("API error (%s): %s", status, msg)
    return jsonify({"success": False, "data": None, "error": msg}), status


def _dates():
    to_date = request.args.get("to_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    from_date = request.args.get("from_date") or (
        datetime.now(timezone.utc) - timedelta(days=cfg.sync_default_period_days)
    ).strftime("%Y-%m-%d")
    return from_date + "T00:00:00Z", to_date + "T23:59:59Z"


def _employee_filter() -> list[str]:
    team_id = db.get_selected_team()
    if team_id:
        return db.get_team_members(team_id)
    return db.get_selected_employees()


def _client_from_db():
    s = db.get_settings()
    if not s.get("pat") or not s.get("collection"):
        return None, "PAT или коллекция не настроены. Откройте /settings"
    client = TFSClient(
        url=s.get("tfs_url") or cfg.tfs_url,
        pat=s["pat"],
        collection=s["collection"],
        api_version=cfg.tfs_api_version,
        timeout=cfg.tfs_timeout,
        verify_ssl=cfg.tfs_verify_ssl,
    )
    return client, None


# ── page routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/developer/<path:email>")
def developer(email: str):
    return render_template("developer.html", email=email)


@app.route("/repos")
def repos():
    return render_template("repos.html")


@app.route("/settings")
def settings():
    s = db.get_settings()
    return render_template(
        "settings.html",
        tfs_url=s.get("tfs_url") or cfg.tfs_url,
        collection=s.get("collection", ""),
        has_pat=bool(s.get("pat")),
    )


# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return _ok({"status": "ok"})


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        s = db.get_settings()
        return _ok({
            "tfs_url": s.get("tfs_url") or cfg.tfs_url,
            "collection": s.get("collection", ""),
            "has_pat": bool(s.get("pat")),
        })
    data = request.get_json(force=True) or {}
    tfs_url = (data.get("tfs_url") or cfg.tfs_url).rstrip("/")
    collection = data.get("collection", "").strip()
    pat = data.get("pat", "").strip()
    if not collection:
        return _err("Коллекция не может быть пустой")
    if not pat:
        return _err("PAT не может быть пустым")
    db.save_settings(tfs_url, collection, pat)
    return _ok({"saved": True})


@app.route("/api/test-connection")
def api_test_connection():
    client, err = _client_from_db()
    if err:
        return _err(err)
    ok, msg = client.test_connection()
    if ok:
        return _ok({"message": msg})
    return _err(msg, 502)


@app.route("/api/employees/all")
def api_employees_all():
    cached = _cache_get("employees_all")
    if cached is None:
        cached = db.get_all_authors()
        _cache_set("employees_all", cached)
    selected = set(db.get_selected_employees())
    result = [dict(a, selected=(a["author_email"] in selected)) for a in cached]
    return _ok(result)


@app.route("/api/all-emails")
def api_all_emails():
    """All unique emails: commit authors + PR creators + reviewers (for alias form)."""
    return _ok(db.get_all_emails())


@app.route("/api/employees/selected", methods=["GET", "POST"])
def api_employees_selected():
    if request.method == "GET":
        return _ok(db.get_selected_employees())
    data = request.get_json(force=True) or {}
    emails = data.get("emails", [])
    if not isinstance(emails, list):
        return _err("emails должен быть массивом")
    db.save_selected_employees(emails)
    return _ok({"saved": len(emails)})


@app.route("/api/teams", methods=["GET", "POST"])
def api_teams():
    if request.method == "GET":
        return _ok(db.get_teams())
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return _err("Название команды не может быть пустым")
    team_id = db.create_team(name)
    if team_id is None:
        return _err("Команда с таким названием уже существует")
    return _ok({"id": team_id, "name": name, "members": []})


@app.route("/api/teams/<int:team_id>", methods=["PUT", "DELETE"])
def api_team(team_id: int):
    if request.method == "DELETE":
        db.delete_team(team_id)
        return _ok({"deleted": team_id})
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return _err("Название не может быть пустым")
    ok = db.rename_team(team_id, name)
    if not ok:
        return _err("Команда с таким названием уже существует")
    return _ok({"id": team_id, "name": name})


@app.route("/api/teams/<int:team_id>/members", methods=["POST", "DELETE"])
def api_team_members(team_id: int):
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return _err("email не может быть пустым")
    if request.method == "POST":
        db.add_team_member(team_id, email)
        return _ok({"added": email})
    db.remove_team_member(team_id, email)
    return _ok({"removed": email})


@app.route("/api/teams/select", methods=["GET", "POST"])
def api_teams_select():
    if request.method == "GET":
        team_id = db.get_selected_team()
        team_name = None
        if team_id:
            teams = {t["id"]: t["name"] for t in db.get_teams()}
            team_name = teams.get(team_id)
        return _ok({"team_id": team_id, "team_name": team_name})
    data = request.get_json(force=True) or {}
    try:
        team_id = int(data.get("team_id", 0))
    except (TypeError, ValueError):
        team_id = 0
    db.save_selected_team(team_id)
    _cache_clear()
    return _ok({"team_id": team_id})


@app.route("/api/display-names", methods=["GET", "POST"])
def api_display_names():
    if request.method == "GET":
        return _ok(db.get_display_names())
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    display_name = (data.get("display_name") or "").strip()
    if not email or not display_name:
        return _err("email and display_name required")
    db.set_display_name(email, display_name)
    return _ok({"email": email, "display_name": display_name})


@app.route("/api/display-names/<path:email>", methods=["DELETE"])
def api_display_name_delete(email: str):
    db.delete_display_name(email)
    return _ok(None)


@app.route("/api/overview")
def api_overview():
    from_date, to_date = _dates()
    employees = _employee_filter()
    data = db.get_overview(from_date, to_date, employees)
    data["cached_range"] = db.get_cached_range()
    data["employee_filter_active"] = bool(employees)
    data["employee_filter_count"] = len(employees)
    return _ok(data)


@app.route("/api/monthly-stats")
def api_monthly_stats():
    from_date, to_date = _dates()
    employees = _employee_filter()
    data = db.get_monthly_stats(from_date, to_date, employees)
    return _ok(data)


@app.route("/api/developers")
def api_developers():
    from_date, to_date = _dates()
    employees = _employee_filter()
    cache_key = f"devs|{from_date}|{to_date}|{','.join(sorted(employees))}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return _ok(cached)
    # Run both queries concurrently (WAL allows parallel reads)
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_devs = ex.submit(db.get_developers, from_date, to_date, employees)
        f_wi   = ex.submit(db.get_work_item_stats_all, from_date, to_date, employees)
        devs     = f_devs.result()
        wi_stats = f_wi.result()
    for d in devs:
        wi = wi_stats.get(d["author_email"], {})
        d["wi_created"]  = wi.get("created",  0)
        d["wi_resolved"] = wi.get("resolved", 0)
        d["wi_closed"]   = wi.get("closed",   0)
    _cache_set(cache_key, devs)
    return _ok(devs)


@app.route("/api/developer/<path:email>")
def api_developer(email: str):
    from_date, to_date = _dates()
    email = db.get_canonical_email(email)
    data = db.get_developer_stats(email, from_date, to_date)
    data["work_items"] = db.get_developer_work_item_stats(email, from_date, to_date)
    return _ok(data)


@app.route("/api/compare")
def api_compare():
    emails_raw = request.args.get("emails", "")
    emails = [e.strip() for e in emails_raw.split(",") if e.strip()]
    if not emails:
        return _err("Не указаны email разработчиков (параметр emails)")
    if len(emails) > 5:
        return _err("Максимум 5 разработчиков для сравнения")
    from_date, to_date = _dates()
    return _ok(db.get_compare_stats(emails, from_date, to_date))


@app.route("/api/repositories")
def api_repositories():
    from_date, to_date = _dates()
    return _ok(db.get_repositories(from_date, to_date, _employee_filter()))


@app.route("/api/repository/<repo_id>")
def api_repository(repo_id: str):
    from_date, to_date = _dates()
    return _ok(db.get_repository_stats(repo_id, from_date, to_date, _employee_filter()))


@app.route("/api/projects")
def api_projects():
    client, err = _client_from_db()
    if err:
        return _err(err)
    try:
        projects = client.get_projects()
        selected = db.get_selected_projects()
        return _ok([
            {"id": p["id"], "name": p["name"], "selected": p["id"] in selected}
            for p in projects
        ])
    except Exception as e:
        log.exception("Ошибка получения списка проектов")
        return _err(str(e), 502)


@app.route("/api/projects/selected", methods=["POST"])
def api_save_selected_projects():
    data = request.get_json(force=True) or {}
    ids = data.get("project_ids", [])
    if not isinstance(ids, list):
        return _err("project_ids должен быть массивом")
    db.save_selected_projects(ids)
    return _ok({"saved": len(ids)})


@app.route("/api/aliases", methods=["GET", "POST"])
def api_aliases():
    if request.method == "GET":
        return _ok(db.get_alias_groups())
    data = request.get_json(force=True) or {}
    primary = (data.get("primary_email") or "").strip()
    alias   = (data.get("alias_email") or "").strip()
    if not primary or not alias:
        return _err("primary_email и alias_email обязательны")
    err = db.add_alias(primary, alias)
    if err:
        return _err(err)
    db.rebuild_login_map()
    _cache_clear()
    return _ok({"added": True})


@app.route("/api/aliases/<path:alias_email>", methods=["DELETE"])
def api_alias_delete(alias_email: str):
    db.remove_alias(alias_email)
    db.rebuild_login_map()
    _cache_clear()
    return _ok({"removed": True})


@app.route("/api/alias-group/<path:primary_email>", methods=["DELETE"])
def api_alias_group_delete(primary_email: str):
    db.remove_alias_group(primary_email)
    db.rebuild_login_map()
    _cache_clear()
    return _ok({"removed": True})


@app.route("/api/clear-data", methods=["POST"])
def api_clear_data():
    if get_sync_status()["running"]:
        return _err("Нельзя очищать данные во время синхронизации", 409)
    db.clear_data()
    log.info("Data cleared via API")
    return _ok({"cleared": True})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    if get_sync_status()["running"]:
        return _err("Синхронизация уже выполняется", 409)
    data = request.get_json(force=True) or {}
    incremental = bool(data.get("incremental", False))
    to_dt = datetime.now(timezone.utc)
    days = int(data.get("days", cfg.sync_default_period_days))
    from_dt = to_dt - timedelta(days=days)
    from_date = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    to_date = to_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    _cache_clear()
    start_sync_async(db, from_date, to_date, incremental=incremental)
    if incremental:
        msg = f"Инкрементальная синхронизация запущена (fallback: {days} дней)"
    else:
        msg = f"Синхронизация запущена за {days} дней"
    return _ok({"message": msg, "from": from_date, "to": to_date, "incremental": incremental})


@app.route("/api/sync-status")
def api_sync_status():
    return _ok(get_sync_status())


@app.route("/api/sync-log")
def api_sync_log():
    limit = int(request.args.get("limit", 50))
    return _ok(db.get_sync_log(limit))

@app.route("/api/debug/prs")
def api_debug_prs():
    with db._conn() as conn:
        rows = conn.execute("SELECT id, creator_email, created_date, status FROM pull_requests LIMIT 20").fetchall()
        count = conn.execute("SELECT COUNT(*) FROM pull_requests").fetchone()[0]
    return _ok({"total": count, "sample": [dict(r) for r in rows]})

@app.route("/api/debug/pr-raw")
def api_debug_pr_raw():
    client, err = _client_from_db()
    if err:
        return _err(err)
    s = db.get_settings()
    projects = client.get_projects()
    selected_ids = db.get_selected_projects()
    projects = [p for p in projects if p["id"] in selected_ids] if selected_ids else projects
    for proj in projects[:1]:
        repos = client.get_repositories(proj["id"])
        for repo in repos[:1]:
            import requests as req
            url = f"{client.base}/{client.collection}/{proj['id']}/_apis/git/repositories/{repo['id']}/pullrequests"
            resp = client._get(url, {"$top": 1, "searchCriteria.status": "all"})
            prs = resp.get("value", [])
            if prs:
                return _ok({"createdBy_raw": prs[0].get("createdBy", {})})
    return _ok({"message": "no prs found"})


# ── startup ───────────────────────────────────────────────────────────────────

def shutdown(signum, frame):
    log.info("Завершение работы (сигнал %s)...", signum)
    stop_scheduler()
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info("=" * 60)
    log.info("AzureDevOps Pulse запускается")
    log.info("TFS URL (из конфига): %s", cfg.tfs_url)
    log.info("БД: %s", cfg.database_path)
    log.info("Порт: %s", cfg.server_port)
    log.info("=" * 60)

    s = db.get_settings()
    if s.get("pat") and s.get("collection"):
        log.info("Настройки найдены, запускаем планировщик")
        start_scheduler(db, cfg.sync_interval_hours, cfg.sync_default_period_days)
    else:
        log.warning("PAT/коллекция не настроены. Откройте http://localhost:%d/settings", cfg.server_port)

    app.run(
        host=cfg.server_host,
        port=cfg.server_port,
        debug=False,
        use_reloader=False,
    )
