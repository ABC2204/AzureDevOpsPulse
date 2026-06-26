import base64
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import requests
import urllib3
from requests.adapters import HTTPAdapter

from database import Database
from logger import get_logger

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = get_logger("collector")

_MERGE_KEYWORDS = (
    "merge pull request",
    "merged pr",
    "merged in ",
    "merge branch",
    "merge remote-tracking",
)

# Размер страницы при пагинации.
# TFS Git Commits: официальный лимит $top=1000, выбираем 500 — баланс.
# TFS Pull Requests: многие версии TFS Server ограничивают $top=200.
_COMMITS_PAGE_SIZE = 500
_PR_PAGE_SIZE = 200

# Параметры retry/backoff для сетевых запросов.
_MAX_RETRIES = 3          # количество попыток
_RETRY_STATUSES = {429, 500, 502, 503, 504}  # HTTP-коды, при которых повторяем

# Параметры пула соединений HTTP-адаптера.
_HTTP_POOL_SIZE = 20


def _is_merge(comment: str) -> bool:
    c = (comment or "").lower()
    return any(k in c for k in _MERGE_KEYWORDS)


def _parse_date(s: str) -> Optional[str]:
    if not s:
        return None
    try:
        clean_s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_s)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return s[:19].replace("T", "T") + "Z" if len(s) >= 19 else s


def _resolve_email(
    mail: str,
    unique: str,
    display_name: str,
    name_to_email: dict,
    login_to_email: dict = None,
    db: "Database" = None,
) -> str:
    """Резолвит email пользователя TFS из доступных полей.

    Порядок: mailAddress → uniqueName с @ → NT-login по displayName →
             NT-login по login_to_email кэшу → NT-login по LIKE в commits (fallback) →
             голый логин.
    """
    if mail:
        return mail.lower()
    if unique and "@" in unique:
        return unique.lower()
    if unique and "\\" in unique:
        login = unique.split("\\")[-1].lower()
        if display_name and display_name.lower() in name_to_email:
            return name_to_email[display_name.lower()]
        # Быстрый путь: login_map_cache (предзагружен, без обращений к БД)
        if login_to_email and login in login_to_email:
            return login_to_email[login]
        # Fallback: LIKE-запрос в БД (только когда кэш не передан или не покрывает)
        if db is not None:
            with db._conn() as conn:
                row = conn.execute(
                    "SELECT author_email FROM commits "
                    "WHERE LOWER(author_email) LIKE ? AND author_email != '' LIMIT 1",
                    (f"{login}@%",)
                ).fetchone()
            if row:
                return row[0].lower()
        return login
    return ""


class TFSClient:
    def __init__(
        self,
        url: str,
        pat: str,
        collection: str,
        api_version: str = "7.2-preview",
        timeout: int = 30,
        verify_ssl: bool = False,
    ):
        self.base = url.rstrip("/")
        self.collection = collection
        self.api_version = api_version
        self.timeout = timeout
        self.verify = verify_ssl
        token = base64.b64encode(f":{pat}".encode()).decode()
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
        )
        self.session.verify = verify_ssl
        # Увеличиваем пул соединений: при параллельной синхронизации репо
        # дефолтный pool_maxsize=10 становится узким местом.
        adapter = HTTPAdapter(pool_connections=_HTTP_POOL_SIZE, pool_maxsize=_HTTP_POOL_SIZE)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def _url(self, *parts: str) -> str:
        return "/".join([self.base, self.collection, *parts])

    def _get(self, url: str, params: dict = None) -> dict:
        """GET с экспоненциальным backoff при сетевых ошибках и 5xx/429."""
        p = {"api-version": self.api_version, **(params or {})}
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(_MAX_RETRIES):
            t0 = time.time()
            try:
                resp = self.session.get(url, params=p, timeout=self.timeout)
                elapsed = round(time.time() - t0, 2)
                log.debug("GET %s → %s (%.2fs)", url, resp.status_code, elapsed)
                if resp.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                    delay = 2 ** attempt
                    log.warning("HTTP %s на GET %s, retry %d/%d через %ds",
                                resp.status_code, url, attempt + 1, _MAX_RETRIES - 1, delay)
                    time.sleep(delay)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError as e:
                if e.response.status_code not in _RETRY_STATUSES or attempt == _MAX_RETRIES - 1:
                    log.error("HTTP %s на GET %s — body: %s",
                              e.response.status_code, url, e.response.text[:500])
                    raise
                last_exc = e
                delay = 2 ** attempt
                log.warning("HTTP %s на GET %s, retry %d/%d через %ds",
                            e.response.status_code, url, attempt + 1, _MAX_RETRIES - 1, delay)
                time.sleep(delay)
            except requests.RequestException as e:
                last_exc = e
                if attempt < _MAX_RETRIES - 1:
                    delay = 2 ** attempt
                    log.warning("Сетевая ошибка GET %s: %s — retry %d/%d через %ds",
                                url, e, attempt + 1, _MAX_RETRIES - 1, delay)
                    time.sleep(delay)
                else:
                    log.error("Request failed: GET %s — %s", url, e)
                    raise
        raise last_exc

    def _post(self, url: str, body: dict, params: dict = None) -> dict:
        """POST с экспоненциальным backoff при сетевых ошибках и 5xx/429."""
        p = {"api-version": self.api_version, **(params or {})}
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(_MAX_RETRIES):
            try:
                resp = self.session.post(url, json=body, params=p, timeout=self.timeout)
                if resp.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                    delay = 2 ** attempt
                    log.warning("HTTP %s на POST %s, retry %d/%d через %ds",
                                resp.status_code, url, attempt + 1, _MAX_RETRIES - 1, delay)
                    time.sleep(delay)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError as e:
                if e.response.status_code not in _RETRY_STATUSES or attempt == _MAX_RETRIES - 1:
                    log.error("HTTP %s на POST %s — body: %s",
                              e.response.status_code, url, e.response.text[:500])
                    raise
                last_exc = e
                delay = 2 ** attempt
                log.warning("HTTP %s на POST %s, retry %d/%d через %ds",
                            e.response.status_code, url, attempt + 1, _MAX_RETRIES - 1, delay)
                time.sleep(delay)
            except requests.RequestException as e:
                last_exc = e
                if attempt < _MAX_RETRIES - 1:
                    delay = 2 ** attempt
                    log.warning("Сетевая ошибка POST %s: %s — retry %d/%d через %ds",
                                url, e, attempt + 1, _MAX_RETRIES - 1, delay)
                    time.sleep(delay)
                else:
                    log.error("Request failed: POST %s — %s", url, e)
                    raise
        raise last_exc

    def get_work_items_for_project(
        self, project_id: str, from_date: str, to_date: str
    ) -> list[dict]:
        """Собирает задачи проекта через WIQL + параллельный batch-fetch полей."""
        from_d = from_date[:10]
        to_d   = to_date[:10]

        wiql_url = self._url(project_id, "_apis/wit/wiql")
        query = (
            f"SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = @project "
            f"AND [System.ChangedDate] >= '{from_d}' "
            f"AND [System.ChangedDate] <= '{to_d}' "
            f"ORDER BY [System.Id]"
        )
        try:
            wiql_resp = self._post(wiql_url, {"query": query})
        except Exception:
            log.warning("WIQL недоступен для проекта %s, пропуск work items", project_id)
            return []

        refs = wiql_resp.get("workItems", [])
        if not refs:
            return []

        ids = [str(r["id"]) for r in refs]
        log.info("Проект %s: найдено %d work items по WIQL", project_id, len(ids))

        fields = [
            "System.Id", "System.WorkItemType", "System.State", "System.Title",
            "System.CreatedBy", "System.CreatedDate",
            "Microsoft.VSTS.Common.ResolvedBy", "Microsoft.VSTS.Common.ResolvedDate",
            "Microsoft.VSTS.Common.ClosedBy",   "Microsoft.VSTS.Common.ClosedDate",
        ]
        batch_url = self._url("_apis/wit/workitems")
        chunks = [ids[i:i + 200] for i in range(0, len(ids), 200)]

        def _fetch_chunk(chunk: list[str]) -> list[dict]:
            try:
                data = self._get(batch_url, {
                    "ids": ",".join(chunk),
                    "fields": ",".join(fields),
                })
                return data.get("value", [])
            except Exception:
                log.warning("Ошибка batch-fetch work items (chunk ids %s...), пропуск", chunk[0])
                return []

        result: list[dict] = []
        # Параллельный fetch чанков (I/O-bound, безопасно для requests.Session с пулом)
        with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as pool:
            futures = [pool.submit(_fetch_chunk, chunk) for chunk in chunks]
            for fut in as_completed(futures):
                result.extend(fut.result())
        return result

    def test_connection(self) -> tuple[bool, str]:
        try:
            url = self._url("_apis/projects")
            data = self._get(url, {"$top": "1"})
            count = data.get("count", 0)
            msg = f"OK — найдено проектов: {count}"
            log.info("TFS connection test: %s", msg)
            return True, msg
        except requests.HTTPError as e:
            code = e.response.status_code
            hints = {
                401: "401 Unauthorized — неверный PAT или истёк срок действия",
                403: "403 Forbidden — PAT не имеет прав на чтение проектов",
                404: "404 Not Found — проверьте URL сервера и название коллекции",
            }
            msg = hints.get(code, f"HTTP {code}: {e.response.text[:200]}")
            return False, msg
        except Exception as e:
            return False, f"Ошибка соединения: {e}"

    def get_projects(self) -> list[dict]:
        url = self._url("_apis/projects")
        result, skip = [], 0
        while True:
            data = self._get(url, {"$top": 200, "$skip": skip})
            items = data.get("value", [])
            result.extend(items)
            if len(items) < 200:
                break
            skip += 200
        log.info("Получено проектов: %d", len(result))
        return result

    def get_repositories(self, project_id: str) -> list[dict]:
        url = self._url(project_id, "_apis/git/repositories")
        data = self._get(url)
        repos = data.get("value", [])
        log.info("Проект %s: репозиториев %d", project_id, len(repos))
        return repos

    def get_commits(
        self,
        project_id: str,
        repo_id: str,
        from_date: str,
        to_date: str,
    ) -> list[dict]:
        url = self._url(project_id, f"_apis/git/repositories/{repo_id}/commits")
        result, skip = [], 0
        while True:
            data = self._get(url, {
                "$top": _COMMITS_PAGE_SIZE,
                "$skip": skip,
                "searchCriteria.fromDate": from_date,
                "searchCriteria.toDate": to_date,
            })
            items = data.get("value", [])
            result.extend(items)
            log.debug("Repo %s: коммиты skip=%d получено=%d", repo_id, skip, len(items))
            if len(items) < _COMMITS_PAGE_SIZE:
                break
            skip += _COMMITS_PAGE_SIZE
        log.info("Repo %s: всего коммитов %d", repo_id, len(result))
        return result

    def get_pull_requests(
        self,
        project_id: str,
        repo_id: str,
        from_date: str,
        to_date: str,
    ) -> list[dict]:
        url = self._url(project_id, f"_apis/git/repositories/{repo_id}/pullrequests")
        result, skip = [], 0
        while True:
            params: dict = {
                "$top": _PR_PAGE_SIZE,
                "$skip": skip,
                "searchCriteria.status": "all",
                "$expand": "reviewers",
            }
            # searchCriteria.minTime поддерживается TFS 2018+.
            # Обрезает на сервере PR старше from_date → меньше трафика и страниц.
            try:
                params["searchCriteria.minTime"] = from_date
            except Exception:
                pass  # на случай неожиданных ошибок — не критично

            data = self._get(url, params)
            items = data.get("value", [])
            if not items:
                break

            # Клиентская фильтрация по дате (для совместимости с TFS < 2018,
            # где minTime может игнорироваться сервером)
            for pr in items:
                created = pr.get("creationDate", "")
                if created and from_date <= created <= to_date:
                    result.append(pr)

            # PR идут от новых к старым; если самый старый старше from_date — дальше нечего качать
            oldest = items[-1].get("creationDate", "")
            if oldest and oldest < from_date:
                log.debug("Repo %s: достигнуты PR старше %s, остановка пагинации", repo_id, from_date)
                break

            if len(items) < _PR_PAGE_SIZE:
                break
            skip += _PR_PAGE_SIZE

        log.info("Repo %s: всего PR %d (отфильтровано по дате)", repo_id, len(result))
        return result


def sync_repository(
    client: TFSClient,
    db: Database,
    project_id: str,
    project_name: str,
    repo: dict,
    from_date: str,
    to_date: str,
    collection: str,
    name_to_email: dict = None,
    login_to_email: dict = None,
):
    """Синхронизирует один репозиторий (коммиты + PR + ревью).

    name_to_email  — {lower(author_name): email} — предзагружен в run_sync.
    login_to_email — {login: canonical_email} из login_map_cache — предзагружен в run_sync.
    Если не переданы — строятся локально (fallback для прямых вызовов).
    """
    repo_id = repo["id"]
    repo_name = repo["name"]
    log.info("Синхронизация: %s / %s", project_name, repo_name)

    if name_to_email is None:
        name_to_email = {}
    if login_to_email is None:
        login_to_email = {}

    db.upsert_project(project_id, project_name, collection)
    db.upsert_repository(repo_id, project_id, repo_name, repo.get("defaultBranch", ""))

    # ── Коммиты ──────────────────────────────────────────────────────────────
    commit_count = 0
    commits: list[dict] = []
    try:
        commits = client.get_commits(project_id, repo_id, from_date, to_date)
        commit_rows = []
        for c in commits:
            author = c.get("author") or {}
            committer = c.get("committer") or {}
            changes = c.get("changeCounts") or {}
            commit_rows.append((
                c["commitId"],
                repo_id,
                (author.get("email", "") or "").lower(),
                author.get("name", ""),
                _parse_date(author.get("date", "")),
                committer.get("email", ""),
                committer.get("name", ""),
                _parse_date(committer.get("date", "")),
                c.get("comment", "")[:500],
                changes.get("Add", 0),
                changes.get("Edit", 0),
                changes.get("Delete", 0),
                1 if _is_merge(c.get("comment", "")) else 0,
            ))
            commit_count += 1
        # Один batch-insert вместо N отдельных транзакций
        db.upsert_commits_batch(commit_rows)
        db.log_sync(repo_id, "commits", commit_count)
    except Exception:
        log.exception("Ошибка коммитов для %s/%s", project_name, repo_name)
        db.log_sync(repo_id, "commits", 0, "см. лог")

    # Дополняем name_to_email только что скачанными данными (новые разработчики).
    # Делаем локальную копию, чтобы не мутировать глобальный словарь.
    local_name_to_email = dict(name_to_email)
    for c in commits:
        author = c.get("author") or {}
        a_name = (author.get("name") or "").lower()
        a_email = (author.get("email") or "").lower()
        if a_name and a_email:
            local_name_to_email[a_name] = a_email

    # ── Pull Requests + ревьюеры ─────────────────────────────────────────────
    pr_count = 0
    try:
        prs = client.get_pull_requests(project_id, repo_id, from_date, to_date)
        pr_rows = []
        review_rows = []
        for pr in prs:
            creator = pr.get("createdBy") or {}
            display_name = creator.get("displayName", "")
            creator_email = _resolve_email(
                creator.get("mailAddress", "").strip(),
                creator.get("uniqueName", "").strip(),
                display_name,
                local_name_to_email,
                login_to_email,
            )
            pr_id = pr["pullRequestId"]
            pr_rows.append((
                pr_id, repo_id, project_id,
                pr.get("title", "")[:300],
                (creator_email or "").lower(),
                display_name,
                pr.get("status", ""),
                _parse_date(pr.get("creationDate", "")),
                _parse_date(pr.get("closedDate")),
                pr.get("targetRefName", ""),
                pr.get("sourceRefName", ""),
            ))
            for reviewer in pr.get("reviewers") or []:
                r_name = reviewer.get("displayName", "")
                r_email = _resolve_email(
                    reviewer.get("mailAddress", "").strip(),
                    reviewer.get("uniqueName", "").strip(),
                    r_name,
                    local_name_to_email,
                    login_to_email,
                )
                if r_email:
                    review_rows.append((pr_id, (r_email or "").lower(), r_name, reviewer.get("vote", 0)))
            pr_count += 1
        # Два batch-insert вместо M+K отдельных транзакций
        db.upsert_pull_requests_batch(pr_rows)
        db.upsert_pr_reviews_batch(review_rows)
        db.log_sync(repo_id, "pull_requests", pr_count)
    except Exception:
        log.exception("Ошибка PR для %s/%s", project_name, repo_name)
        db.log_sync(repo_id, "pull_requests", 0, "см. лог")

    db.mark_repo_synced(repo_id)
    log.info("Готово: %s / %s — коммитов %d, PR %d", project_name, repo_name, commit_count, pr_count)


def _parse_wi_identity(
    field,
    name_to_email: dict,
    login_to_email: dict = None,
    db: "Database" = None,
) -> str:
    """Парсит поле-идентификатор work item (dict или строку) в email."""
    if not field:
        return ""
    if isinstance(field, dict):
        mail    = field.get("mailAddress", "").strip()
        unique  = field.get("uniqueName", "").strip()
        display = field.get("displayName", "").strip()
        return _resolve_email(mail, unique, display, name_to_email, login_to_email, db)
    # Строка вида "Display Name <email>" или просто "login"
    s = str(field).strip()
    m = re.search(r"<([^>]+)>", s)
    if m:
        return m.group(1).lower()
    if "@" in s:
        return s.lower()
    return _resolve_email("", s, "", name_to_email, login_to_email, db)


def sync_work_items(
    client: TFSClient,
    db: Database,
    project_id: str,
    from_date: str,
    to_date: str,
    name_to_email: dict = None,
    login_to_email: dict = None,
):
    """Синхронизирует work items для одного проекта.

    name_to_email  — предзагружен в run_sync (не строится здесь заново).
    login_to_email — предзагружен в run_sync.
    """
    log.info("Work items: синхронизация проекта %s", project_id)

    if name_to_email is None:
        # Fallback: строим локально (прямой вызов без кэша)
        name_to_email = db.get_name_to_email_map()
    if login_to_email is None:
        login_to_email = db.get_login_to_email_map()

    try:
        items = client.get_work_items_for_project(project_id, from_date, to_date)
    except Exception:
        log.exception("Ошибка получения work items для проекта %s", project_id)
        return

    wi_rows = []
    for wi in items:
        f = wi.get("fields", {})
        wi_id = wi.get("id")
        if not wi_id:
            continue
        wi_rows.append((
            wi_id,
            project_id,
            f.get("System.WorkItemType", ""),
            f.get("System.State", ""),
            (f.get("System.Title") or "")[:300],
            _parse_wi_identity(f.get("System.CreatedBy"), name_to_email, login_to_email),
            _parse_date(f.get("System.CreatedDate")),
            _parse_wi_identity(f.get("Microsoft.VSTS.Common.ResolvedBy"), name_to_email, login_to_email),
            _parse_date(f.get("Microsoft.VSTS.Common.ResolvedDate")),
            _parse_wi_identity(f.get("Microsoft.VSTS.Common.ClosedBy"), name_to_email, login_to_email),
            _parse_date(f.get("Microsoft.VSTS.Common.ClosedDate")),
        ))

    db.upsert_work_items_batch(wi_rows)
    db.log_sync(project_id, "work_items", len(wi_rows))
    log.info("Work items: проект %s — сохранено %d записей", project_id, len(wi_rows))
