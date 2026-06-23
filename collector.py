import base64
import time
from datetime import datetime, timezone
from typing import Optional

import requests
import urllib3

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


def _resolve_email(mail: str, unique: str, display_name: str,
                   name_to_email: dict, db: "Database" = None) -> str:
    """Резолвит email пользователя TFS из доступных полей.
    Порядок: mailAddress → uniqueName с @ → NT-login по displayName →
             NT-login по LIKE в commits → голый логин.
    """
    if mail:
        return mail.lower()
    if unique and "@" in unique:
        return unique.lower()
    if unique and "\\" in unique:
        login = unique.split("\\")[-1].lower()
        if display_name and display_name.lower() in name_to_email:
            return name_to_email[display_name.lower()]
        # Ищем по логину в commits (логин совпадает с началом email)
        if db is not None:
            with db._conn() as conn:
                row = conn.execute(
                    "SELECT author_email FROM commits "
                    "WHERE LOWER(author_email) LIKE ? AND author_email != '' LIMIT 1",
                    (f"{login}@%",)
                ).fetchone()
            if row:
                return row[0].lower()
        return login  # последний fallback
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

    def _url(self, *parts: str) -> str:
        return "/".join([self.base, self.collection, *parts])

    def _get(self, url: str, params: dict = None) -> dict:
        p = {"api-version": self.api_version, **(params or {})}
        t0 = time.time()
        try:
            resp = self.session.get(url, params=p, timeout=self.timeout)
            elapsed = round(time.time() - t0, 2)
            log.debug("GET %s → %s (%.2fs)", url, resp.status_code, elapsed)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            log.error("HTTP %s on GET %s — body: %s",
                      e.response.status_code, url, e.response.text[:500])
            raise
        except requests.RequestException as e:
            log.error("Request failed: GET %s — %s", url, e)
            raise

    def _post(self, url: str, body: dict, params: dict = None) -> dict:
        p = {"api-version": self.api_version, **(params or {})}
        try:
            resp = self.session.post(url, json=body, params=p, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            log.error("HTTP %s on POST %s — body: %s",
                      e.response.status_code, url, e.response.text[:500])
            raise
        except requests.RequestException as e:
            log.error("Request failed: POST %s — %s", url, e)
            raise

    def get_work_items_for_project(
        self, project_id: str, from_date: str, to_date: str
    ) -> list[dict]:
        """Собирает задачи проекта через WIQL + batch-fetch полей."""
        # Даты в формате WIQL: 'yyyy-MM-dd'
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
        result = []
        # TFS ограничивает batch до 200 id за раз
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            try:
                data = self._get(batch_url, {
                    "ids": ",".join(chunk),
                    "fields": ",".join(fields),
                })
                result.extend(data.get("value", []))
            except Exception:
                log.warning("Ошибка batch-fetch work items (chunk %d), пропуск", i)
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
                "$top": 100,
                "$skip": skip,
                "searchCriteria.fromDate": from_date,
                "searchCriteria.toDate": to_date,
            })
            items = data.get("value", [])
            result.extend(items)
            log.debug("Repo %s: коммиты skip=%d получено=%d", repo_id, skip, len(items))
            if len(items) < 100:
                break
            skip += 100
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
            data = self._get(url, {
                "$top": 100,
                "$skip": skip,
                "searchCriteria.status": "all",
                "$expand": "reviewers",
            })
            items = data.get("value", [])
            if not items:
                break

            # Фильтруем по дате на стороне клиента — API фильтрацию по дате не поддерживает
            for pr in items:
                created = pr.get("creationDate", "")
                if created and from_date <= created <= to_date:
                    result.append(pr)

            # PR идут от новых к старым; если самый старый старше from_date — дальше нечего качать
            oldest = items[-1].get("creationDate", "")
            if oldest and oldest < from_date:
                log.debug("Repo %s: достигнуты PR старше %s, остановка пагинации", repo_id, from_date)
                break

            if len(items) < 100:
                break
            skip += 100

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
):
    repo_id = repo["id"]
    repo_name = repo["name"]
    log.info("Синхронизация: %s / %s", project_name, repo_name)

    db.upsert_project(project_id, project_name, collection)
    db.upsert_repository(repo_id, project_id, repo_name, repo.get("defaultBranch", ""))

    # Коммиты
    commit_count = 0
    try:
        commits = client.get_commits(project_id, repo_id, from_date, to_date)
        for c in commits:
            author = c.get("author") or {}
            committer = c.get("committer") or {}
            changes = c.get("changeCounts") or {}
            db.upsert_commit(
                id=c["commitId"],
                repo_id=repo_id,
                author_email=author.get("email", ""),
                author_name=author.get("name", ""),
                author_date=_parse_date(author.get("date", "")),
                committer_email=committer.get("email", ""),
                committer_name=committer.get("name", ""),
                committer_date=_parse_date(committer.get("date", "")),
                comment=c.get("comment", "")[:500],
                changes_add=changes.get("Add", 0),
                changes_edit=changes.get("Edit", 0),
                changes_delete=changes.get("Delete", 0),
                is_merge=1 if _is_merge(c.get("comment", "")) else 0,
            )
            commit_count += 1
        db.log_sync(repo_id, "commits", commit_count)
    except Exception:
        log.exception("Ошибка коммитов для %s/%s", project_name, repo_name)
        db.log_sync(repo_id, "commits", 0, "см. лог")

    # Строим маппинг displayName → email из коммитов (нужен для резолва NT-логинов)
    name_to_email: dict[str, str] = {}
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT LOWER(author_name), author_email FROM commits "
            "WHERE repo_id=? AND author_email != ''",
            (repo_id,)
        ).fetchall()
        for name, email in rows:
            if name and email:
                name_to_email[name] = email

    # Pull Requests + ревьюеры
    pr_count = 0
    try:
        prs = client.get_pull_requests(project_id, repo_id, from_date, to_date)
        for pr in prs:
            creator = pr.get("createdBy") or {}
            display_name = creator.get("displayName", "")
            creator_email = _resolve_email(
                creator.get("mailAddress", "").strip(),
                creator.get("uniqueName", "").strip(),
                display_name,
                name_to_email,
                db,
            )

            pr_id = pr["pullRequestId"]
            db.upsert_pull_request(
                id=pr_id,
                repo_id=repo_id,
                project_id=project_id,
                title=pr.get("title", "")[:300],
                creator_email=creator_email,
                creator_name=display_name,
                status=pr.get("status", ""),
                created_date=_parse_date(pr.get("creationDate", "")),
                closed_date=_parse_date(pr.get("closedDate")),
                target_branch=pr.get("targetRefName", ""),
                source_branch=pr.get("sourceRefName", ""),
            )

            for reviewer in pr.get("reviewers") or []:
                r_name = reviewer.get("displayName", "")
                r_email = _resolve_email(
                    reviewer.get("mailAddress", "").strip(),
                    reviewer.get("uniqueName", "").strip(),
                    r_name,
                    name_to_email,
                    db,
                )
                if r_email:
                    db.upsert_pr_review(
                        pr_id=pr_id,
                        reviewer_email=r_email,
                        reviewer_name=r_name,
                        vote=reviewer.get("vote", 0),
                    )

            pr_count += 1
        db.log_sync(repo_id, "pull_requests", pr_count)
    except Exception:
        log.exception("Ошибка PR для %s/%s", project_name, repo_name)
        db.log_sync(repo_id, "pull_requests", 0, "см. лог")

    db.mark_repo_synced(repo_id)
    log.info("Готово: %s / %s — коммитов %d, PR %d", project_name, repo_name, commit_count, pr_count)


def _parse_wi_identity(field, name_to_email: dict, db: "Database") -> str:
    """Парсит поле-идентификатор work item (dict или строку) в email."""
    if not field:
        return ""
    if isinstance(field, dict):
        mail    = field.get("mailAddress", "").strip()
        unique  = field.get("uniqueName", "").strip()
        display = field.get("displayName", "").strip()
        return _resolve_email(mail, unique, display, name_to_email, db)
    # Строка вида "Display Name <email>" или просто "login"
    s = str(field).strip()
    import re
    m = re.search(r"<([^>]+)>", s)
    if m:
        return m.group(1).lower()
    if "@" in s:
        return s.lower()
    return _resolve_email("", s, "", name_to_email, db)


def sync_work_items(
    client: TFSClient,
    db: Database,
    project_id: str,
    from_date: str,
    to_date: str,
):
    """Синхронизирует work items для одного проекта."""
    log.info("Work items: синхронизация проекта %s", project_id)

    # Маппинг displayName → email из накопленных коммитов
    name_to_email: dict[str, str] = {}
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT LOWER(author_name), author_email FROM commits WHERE author_email != ''"
        ).fetchall()
        for name, email in rows:
            if name and email:
                name_to_email[name] = email

    try:
        items = client.get_work_items_for_project(project_id, from_date, to_date)
    except Exception:
        log.exception("Ошибка получения work items для проекта %s", project_id)
        return

    count = 0
    for wi in items:
        f = wi.get("fields", {})
        wi_id = wi.get("id")
        if not wi_id:
            continue

        created_date  = _parse_date(f.get("System.CreatedDate"))
        resolved_date = _parse_date(f.get("Microsoft.VSTS.Common.ResolvedDate"))
        closed_date   = _parse_date(f.get("Microsoft.VSTS.Common.ClosedDate"))

        db.upsert_work_item(
            id=wi_id,
            project_id=project_id,
            type=f.get("System.WorkItemType", ""),
            state=f.get("System.State", ""),
            title=(f.get("System.Title") or "")[:300],
            created_by_email=_parse_wi_identity(f.get("System.CreatedBy"), name_to_email, db),
            created_date=created_date,
            resolved_by_email=_parse_wi_identity(f.get("Microsoft.VSTS.Common.ResolvedBy"), name_to_email, db),
            resolved_date=resolved_date,
            closed_by_email=_parse_wi_identity(f.get("Microsoft.VSTS.Common.ClosedBy"), name_to_email, db),
            closed_date=closed_date,
        )
        count += 1

    db.log_sync(project_id, "work_items", count)
    log.info("Work items: проект %s — сохранено %d записей", project_id, count)
