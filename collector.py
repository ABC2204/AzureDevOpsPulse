# import base64
# import time
# from datetime import datetime, timezone
# from typing import Optional
#
# import requests
# import urllib3
#
# from database import Database
# from logger import get_logger
#
# urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
#
# log = get_logger("collector")
#
# _MERGE_KEYWORDS = (
#     "merge pull request",
#     "merged pr",
#     "merged in ",
#     "merge branch",
#     "merge remote-tracking",
# )
#
#
# def _is_merge(comment: str) -> bool:
#     c = (comment or "").lower()
#     return any(k in c for k in _MERGE_KEYWORDS)
#
#
# class TFSClient:
#     def __init__(
#         self,
#         url: str,
#         pat: str,
#         collection: str,
#         api_version: str = "7.2-preview",
#         timeout: int = 30,
#         verify_ssl: bool = False,
#     ):
#         self.base = url.rstrip("/")
#         self.collection = collection
#         self.api_version = api_version
#         self.timeout = timeout
#         self.verify = verify_ssl
#         token = base64.b64encode(f":{pat}".encode()).decode()
#         self.session = requests.Session()
#         self.session.headers.update(
#             {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
#         )
#         self.session.verify = verify_ssl
#
#     def _url(self, *parts: str) -> str:
#         return "/".join([self.base, self.collection, *parts])
#
#     def _get(self, url: str, params: dict = None) -> dict:
#         p = {"api-version": self.api_version, **(params or {})}
#         t0 = time.time()
#         try:
#             resp = self.session.get(url, params=p, timeout=self.timeout)
#             elapsed = round(time.time() - t0, 2)
#             log.debug("GET %s → %s (%.2fs)", url, resp.status_code, elapsed)
#             resp.raise_for_status()
#             return resp.json()
#         except requests.HTTPError as e:
#             log.error(
#                 "HTTP %s on GET %s — body: %s",
#                 e.response.status_code,
#                 url,
#                 e.response.text[:500],
#             )
#             raise
#         except requests.RequestException as e:
#             log.error("Request failed: GET %s — %s", url, e)
#             raise
#
#     def test_connection(self) -> tuple[bool, str]:
#         try:
#             url = self._url("_apis/projects")
#             data = self._get(url, {"$top": "1"})
#             count = data.get("count", 0)
#             msg = f"OK — найдено проектов: {count}"
#             log.info("TFS connection test: %s", msg)
#             return True, msg
#         except requests.HTTPError as e:
#             code = e.response.status_code
#             hints = {
#                 401: "401 Unauthorized — неверный PAT или истёк срок действия",
#                 403: "403 Forbidden — PAT не имеет прав на чтение проектов",
#                 404: "404 Not Found — проверьте URL сервера и название коллекции",
#             }
#             msg = hints.get(code, f"HTTP {code}: {e.response.text[:200]}")
#             return False, msg
#         except Exception as e:
#             return False, f"Ошибка соединения: {e}"
#
#     def get_projects(self) -> list[dict]:
#         url = self._url("_apis/projects")
#         result, skip = [], 0
#         while True:
#             data = self._get(url, {"$top": 200, "$skip": skip})
#             items = data.get("value", [])
#             result.extend(items)
#             if len(items) < 200:
#                 break
#             skip += 200
#         log.info("Получено проектов: %d", len(result))
#         return result
#
#     def get_repositories(self, project_id: str) -> list[dict]:
#         url = self._url(project_id, "_apis/git/repositories")
#         data = self._get(url)
#         repos = data.get("value", [])
#         log.info("Проект %s: репозиториев %d", project_id, len(repos))
#         return repos
#
#     def get_commits(
#         self,
#         project_id: str,
#         repo_id: str,
#         from_date: str,
#         to_date: str,
#     ) -> list[dict]:
#         url = self._url(project_id, f"_apis/git/repositories/{repo_id}/commits")
#         result, skip = [], 0
#         while True:
#             data = self._get(
#                 url,
#                 {
#                     "$top": 100,
#                     "$skip": skip,
#                     "searchCriteria.fromDate": from_date,
#                     "searchCriteria.toDate": to_date,
#                 },
#             )
#             items = data.get("value", [])
#             result.extend(items)
#             log.debug(
#                 "Repo %s: коммиты skip=%d получено=%d", repo_id, skip, len(items)
#             )
#             if len(items) < 100:
#                 break
#             skip += 100
#         log.info("Repo %s: всего коммитов %d", repo_id, len(result))
#         return result
#
#     def get_pull_requests(
#         self,
#         project_id: str,
#         repo_id: str,
#         from_date: str,
#         to_date: str,
#     ) -> list[dict]:
#         url = self._url(project_id, f"_apis/git/repositories/{repo_id}/pullrequests")
#         result, skip = [], 0
#         while True:
#             data = self._get(
#                 url,
#                 {
#                     "$top": 100,
#                     "$skip": skip,
#                     # status=all (или 4) нужен, чтобы получить и completed, и abandoned, а не только active
#                     "searchCriteria.status": "all",
#                 },
#             )
#             items = data.get("value", [])
#             if not items:
#                 break
#
#             # Фильтруем по дате на стороне клиента, так как API это не поддерживает
#             for pr in items:
#                 created = pr.get("creationDate", "")
#                 if created and from_date <= created <= to_date:
#                     result.append(pr)
#
#             # Оптимизация: PR идут от новых к старым.
#             # Если самый старый в пачке старше from_date, дальше качать нет смысла.
#             oldest_in_batch = items[-1].get("creationDate", "")
#             if oldest_in_batch and oldest_in_batch < from_date:
#                 log.debug("Repo %s: достигнуты PR старше %s, остановка пагинации", repo_id, from_date)
#                 break
#
#             if len(items) < 100:
#                 break
#             skip += 100
#
#         log.info("Repo %s: всего PR %d (отфильтровано по дате)", repo_id, len(result))
#         return result
#
#
# def _parse_date(s: str) -> Optional[str]:
#     if not s:
#         return None
#     try:
#         # Убираем всё, что мешает парсингу, и приводим к UTC
#         clean_s = s.replace("Z", "+00:00")
#         dt = datetime.fromisoformat(clean_s)
#         return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
#     except Exception:
#         # Если парсинг упал, просто берем первые 19 символов и добавляем Z
#         # Это спасет, если дата пришла как "2026-06-17T10:14:11.1234567+03:00"
#         return s[:19].replace("T", "T") + "Z" if len(s) >= 19 else s
#
#
# def sync_repository(
#     client: TFSClient,
#     db: Database,
#     project_id: str,
#     project_name: str,
#     repo: dict,
#     from_date: str,
#     to_date: str,
#     collection: str,
# ):
#     repo_id = repo["id"]
#     repo_name = repo["name"]
#     log.info("Синхронизация: %s / %s", project_name, repo_name)
#
#     db.upsert_project(project_id, project_name, collection)
#     db.upsert_repository(repo_id, project_id, repo_name, repo.get("defaultBranch", ""))
#
#     # commits
#     commit_count = 0
#     try:
#         commits = client.get_commits(project_id, repo_id, from_date, to_date)
#         for c in commits:
#             author = c.get("author") or {}
#             committer = c.get("committer") or {}
#             changes = c.get("changeCounts") or {}
#             db.upsert_commit(
#                 id=c["commitId"],
#                 repo_id=repo_id,
#                 author_email=author.get("email", ""),
#                 author_name=author.get("name", ""),
#                 author_date=_parse_date(author.get("date", "")),
#                 committer_email=committer.get("email", ""),
#                 committer_name=committer.get("name", ""),
#                 committer_date=_parse_date(committer.get("date", "")),
#                 comment=c.get("comment", "")[:500],
#                 changes_add=changes.get("Add", 0),
#                 changes_edit=changes.get("Edit", 0),
#                 changes_delete=changes.get("Delete", 0),
#                 is_merge=1 if _is_merge(c.get("comment", "")) else 0,
#             )
#             commit_count += 1
#         db.log_sync(repo_id, "commits", commit_count)
#     except Exception as e:
#         log.exception("Ошибка коммитов для %s/%s", project_name, repo_name)
#         db.log_sync(repo_id, "commits", 0, str(e))
#
#     # pull requests
#     # pr_count = 0
#     # try:
#     #     prs = client.get_pull_requests(project_id, repo_id, from_date, to_date)
#     #     for pr in prs:
#     #         creator = pr.get("createdBy") or {}
#     #         db.upsert_pull_request(
#     #             id=pr["pullRequestId"],
#     #             repo_id=repo_id,
#     #             project_id=project_id,
#     #             title=pr.get("title", "")[:300],
#     #             creator_email=creator.get("mailAddress", "") or creator.get("uniqueName", ""),
#     #
#     #             # collector.py — было
#     #             # creator_email=creator.get("mailAddress", "") or creator.get("uniqueName", ""),
#     #
#     #             # исправить на: брать только mailAddress, uniqueName использовать только если он содержит @
#     #             # mail=creator.get("mailAddress", "").strip(),
#     #             # unique = creator.get("uniqueName", "").strip(),
#     #             # creator_email = mail or (unique if "@" in unique else ""),
#     #             creator_name=creator.get("displayName", ""),
#     #             status=pr.get("status", ""),
#     #             created_date=_parse_date(pr.get("creationDate", "")),
#     #             closed_date=_parse_date(pr.get("closedDate")),
#     #             target_branch=pr.get("targetRefName", ""),
#     #             source_branch=pr.get("sourceRefName", ""),
#     #         )
#     #         pr_count += 1
#     #     db.log_sync(repo_id, "pull_requests", pr_count)
#     # except Exception as e:
#     #     log.exception("Ошибка PR для %s/%s", project_name, repo_name)
#     #     db.log_sync(repo_id, "pull_requests", 0, str(e))
#
#         # pull requests
#         pr_count = 0
#         try:
#             prs = client.get_pull_requests(project_id, repo_id, from_date, to_date)
#             for pr in prs:
#                 creator = pr.get("createdBy") or {}
#                 mail = creator.get("mailAddress", "").strip()
#                 unique = creator.get("uniqueName", "").strip()
#                 creator_email = mail or (unique if "@" in unique else "")
#                 db.upsert_pull_request(
#                     id=pr["pullRequestId"],
#                     repo_id=repo_id,
#                     project_id=project_id,
#                     title=pr.get("title", "")[:300],
#                     creator_email=creator_email,
#                     creator_name=creator.get("displayName", ""),
#                     status=pr.get("status", ""),
#                     created_date=_parse_date(pr.get("creationDate", "")),
#                     closed_date=_parse_date(pr.get("closedDate")),
#                     target_branch=pr.get("targetRefName", ""),
#                     source_branch=pr.get("sourceRefName", ""),
#                 )
#                 pr_count += 1
#             db.log_sync(repo_id, "pull_requests", pr_count)
#         except Exception as e:
#             log.exception("Ошибка PR для %s/%s", project_name, repo_name)
#             db.log_sync(repo_id, "pull_requests", 0, str(e))
#
#     db.mark_repo_synced(repo_id)
#     log.info("Готово: %s / %s — коммитов %d, PR %d", project_name, repo_name, commit_count, pr_count)


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
            log.error(
                "HTTP %s on GET %s — body: %s",
                e.response.status_code,
                url,
                e.response.text[:500],
            )
            raise
        except requests.RequestException as e:
            log.error("Request failed: GET %s — %s", url, e)
            raise

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
            data = self._get(
                url,
                {
                    "$top": 100,
                    "$skip": skip,
                    "searchCriteria.fromDate": from_date,
                    "searchCriteria.toDate": to_date,
                },
            )
            items = data.get("value", [])
            result.extend(items)
            log.debug(
                "Repo %s: коммиты skip=%d получено=%d", repo_id, skip, len(items)
            )
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
            data = self._get(
                url,
                {
                    "$top": 100,
                    "$skip": skip,
                    # status=all (или 4) нужен, чтобы получить и completed, и abandoned, а не только active
                    "searchCriteria.status": "all",
                },
            )
            items = data.get("value", [])
            if not items:
                break

            # Фильтруем по дате на стороне клиента, так как API это не поддерживает
            for pr in items:
                created = pr.get("creationDate", "")
                if created and from_date <= created <= to_date:
                    result.append(pr)

            # Оптимизация: PR идут от новых к старым.
            # Если самый старый в пачке старше from_date, дальше качать нет смысла.
            oldest_in_batch = items[-1].get("creationDate", "")
            if oldest_in_batch and oldest_in_batch < from_date:
                log.debug("Repo %s: достигнуты PR старше %s, остановка пагинации", repo_id, from_date)
                break

            if len(items) < 100:
                break
            skip += 100

        log.info("Repo %s: всего PR %d (отфильтровано по дате)", repo_id, len(result))
        return result


def _parse_date(s: str) -> Optional[str]:
    if not s:
        return None
    try:
        # Убираем всё, что мешает парсингу, и приводим к UTC
        clean_s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_s)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        # Если парсинг упал, просто берем первые 19 символов и добавляем Z
        # Это спасет, если дата пришла как "2026-06-17T10:14:11.1234567+03:00"
        return s[:19].replace("T", "T") + "Z" if len(s) >= 19 else s


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

    # commits
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
    except Exception as e:
        log.exception("Ошибка коммитов для %s/%s", project_name, repo_name)
        db.log_sync(repo_id, "commits", 0, str(e))


    # pull requests
    # pull requests
    pr_count = 0
    try:
        # Строим маппинг displayName → email из уже загруженных коммитов
        name_to_email = {}
        with db._conn() as conn:
            rows = conn.execute(
                "SELECT LOWER(author_name) AS name, author_email FROM commits WHERE repo_id=? AND author_email != ''",
                (repo_id,)
            ).fetchall()
            for row in rows:
                if row[0] and row[1]:
                    name_to_email[row[0]] = row[1]

        prs = client.get_pull_requests(project_id, repo_id, from_date, to_date)
        for pr in prs:
            creator = pr.get("createdBy") or {}
            display_name = creator.get("displayName", "")
            mail = creator.get("mailAddress", "").strip()
            unique = creator.get("uniqueName", "").strip()

            if mail:
                creator_email = mail
            elif unique and "@" in unique:
                creator_email = unique.lower()
            elif unique and "\\" in unique:
                # NT_WORK\Rudakova_NA → rudakova_na — ищем в коммитах
                login = unique.split("\\")[-1].lower()  # rudakova_na
                # Сначала ищем по имени в коммитах
                creator_email = name_to_email.get(display_name.lower(), "")
                if not creator_email:
                    # Ищем по логину — перебираем все email в коммитах
                    with db._conn() as conn:
                        row = conn.execute(
                            "SELECT author_email FROM commits WHERE LOWER(author_email) LIKE ? AND author_email != '' LIMIT 1",
                            (f"{login}@%",)
                        ).fetchone()
                    creator_email = row[0] if row else login  # fallback: просто логин
            else:
                creator_email = ""

            db.upsert_pull_request(
                id=pr["pullRequestId"],
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
            pr_count += 1
        db.log_sync(repo_id, "pull_requests", pr_count)
    except Exception as e:
        log.exception("Ошибка PR для %s/%s", project_name, repo_name)
        db.log_sync(repo_id, "pull_requests", 0, str(e))


    # pr_count = 0
    # try:
    #     prs = client.get_pull_requests(project_id, repo_id, from_date, to_date)
    #     for pr in prs:
    #         creator = pr.get("createdBy") or {}
    #         mail = creator.get("mailAddress", "").strip()
    #         unique = creator.get("uniqueName", "").strip()
    #         creator_email = mail or (unique if "@" in unique else "")
    #         db.upsert_pull_request(
    #             id=pr["pullRequestId"],
    #             repo_id=repo_id,
    #             project_id=project_id,
    #             title=pr.get("title", "")[:300],
    #             creator_email=creator_email,
    #             creator_name=creator.get("displayName", ""),
    #             status=pr.get("status", ""),
    #             created_date=_parse_date(pr.get("creationDate", "")),
    #             closed_date=_parse_date(pr.get("closedDate")),
    #             target_branch=pr.get("targetRefName", ""),
    #             source_branch=pr.get("sourceRefName", ""),
    #         )
    #         pr_count += 1
    #     db.log_sync(repo_id, "pull_requests", pr_count)
    # except Exception as e:
    #     log.exception("Ошибка PR для %s/%s", project_name, repo_name)
    #     db.log_sync(repo_id, "pull_requests", 0, str(e))
    #
    # db.mark_repo_synced(repo_id)
    # log.info("Готово: %s / %s — коммитов %d, PR %d", project_name, repo_name, commit_count, pr_count)