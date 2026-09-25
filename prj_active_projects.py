#!/usr/bin/env python3
"""prj_active_projects.py — активные проекты сервера по данным портала Projects.

Источник: read-only POST http://www.projects.prj/schedule.html с Basic-аутентификацией
и фильтрами filter=active, cst_string=%, srv_srg_id[]=1|14 (поддержка/лицензия
Parts.Resource). Архивность проверяется отдельно через /admin/projects.html.

Секреты (notify.secret_get):
  opencode.projects.prj-login-user — логин портала;
  opencode.projects.prj-login      — пароль портала.

Кэш: logs/prj_projects_cache.json, TTL — projects_portal.cache_ttl_hours.
Системный прокси игнорируется (портал доступен только напрямую).

Режимы:
  --server p5ru3   проекты сервера в формате для уведомления;
  --json           машинный вывод;
  --refresh        обновить кэш принудительно;
  --extended       добавить группы 53/74/75 (аренда, синхронизатор);
  --all            все проекты кэша без фильтра по серверу.
"""

from __future__ import annotations

import argparse
import base64
import html as html_mod
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from notify import secret_get

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "tg_support_config.json"
CACHE_FILE = BASE_DIR / "logs" / "prj_projects_cache.json"
ADMIN_CACHE_FILE = BASE_DIR / "logs" / "prj_admin_projects.json"
ARCHIVE_CACHE_FILE = BASE_DIR / "logs" / "prj_archive_cache.json"
FMT = "%Y-%m-%d %H:%M:%S"

LOGIN_SERVICE = "opencode.projects.prj-login-user"
PASSWORD_SERVICE = "opencode.projects.prj-login"

DEFAULT_PORTAL: dict = {
    "url": "http://www.projects.prj",
    "groups": [1, 14],
    "extended_groups": [53, 74, 75],
    "extended": True,
    "scope": "hosted",
    "max_projects_per_server": 30,
    "cache_ttl_hours": 6,
    "admin_cache_ttl_hours": 24,
    "admin_page_size": 50000,
    "check_archive": True,
    "timeout": 30,
    "retries": 2,
    "exclude_archive_unknown": False,
}

HEADER_RE = re.compile(r"\[(\d+)\]\s*<strong>([^<]+)</strong>")
SERVER_RE = re.compile(r"www,\s*db:\s*([A-Za-z0-9._-]+)", re.I)
SERVER_FALLBACK_RE = re.compile(r"db:\s*([A-Za-z0-9._-]+)", re.I)
LINK_RE = re.compile(r"""href=["'](https?://[^"']+)["']""", re.I)
BARE_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
SERVICE_ROW_RE = re.compile(r'<tr\s+class="srv_actual_[^"]*">(.*?)</tr>', re.S | re.I)
SERVICE_NAME_RE = re.compile(r'<td[^>]*title="(\d+)"[^>]*>\s*<span[^>]*>(.*?)</span>', re.S | re.I)
SERVICE_DATE_RE = re.compile(
    r'<small title="\d{2}:\d{2}:\d{2}">\s*(\d{2}/\d{2}/\d{2})?\s*</small>'
)
PLACEMENT_RE = re.compile(r"Размещение на сервере\s*([^<·]+)", re.I)
GROUP_RE = re.compile(r'name="srg_id\[\]" value="(\d+)"', re.I)
GROUP_NAME_RE = re.compile(r"<b>([^<]+)</b>")
TAG_RE = re.compile(r"<[^>]+>")
ARCHIVE_RE = re.compile(
    r"""<select[^>]*name=["']?prj_archive["']?[^>]*>(.*?)</select>""",
    re.S | re.I,
)
ARCHIVE_ZERO_RE = re.compile(r"""<option[^>]*value=["']?0["']?[^>]*selected""", re.I)
DOMAIN_RE = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
ADMIN_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S | re.I)
ADMIN_ID_RE = re.compile(r"prj_name=(\d+)")
ADMIN_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)

_AUTH_HEADER: str | None = None


def load_config(path: str | Path | None = None) -> dict:
    target = Path(path) if path else CONFIG_FILE
    try:
        cfg = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cfg = {}
    portal = dict(DEFAULT_PORTAL)
    section = cfg.get("projects_portal")
    if isinstance(section, dict):
        portal.update(section)
    portal["_root"] = cfg
    return portal


def auth_header() -> str:
    global _AUTH_HEADER
    if _AUTH_HEADER is None:
        raw = f"{secret_get(LOGIN_SERVICE)}:{secret_get(PASSWORD_SERVICE)}"
        encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
        _AUTH_HEADER = f"Basic {encoded}"
    return _AUTH_HEADER


def _decode(data: bytes, charset: str | None) -> str:
    for enc in (charset, "utf-8", "cp1251"):
        if not enc:
            continue
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace")


def _request(req: urllib.request.Request, portal: dict) -> str:
    timeout = int(portal["timeout"])
    retries = int(portal["retries"])
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with opener.open(req, timeout=timeout) as resp:
                return _decode(resp.read(), resp.headers.get_content_charset())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise RuntimeError(
                    f"портал Projects: HTTP {e.code} — проверьте учётные данные"
                ) from e
            last = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
        if attempt < retries:
            continue
    raise RuntimeError(f"портал Projects недоступен: {last}")


def _headers() -> dict:
    return {
        "Authorization": auth_header(),
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "text/html,application/xhtml+xml",
    }


def fetch_page(groups: list[int], portal: dict) -> str:
    base = portal["url"].rstrip("/")
    fields = [("filter_button", "1"), ("filter", "active"), ("cst_string", "%")]
    fields += [("srv_srg_id[]", str(g)) for g in groups]
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/schedule.html",
        data=body,
        headers={**_headers(), "Content-Type": "application/x-www-form-urlencoded"},
    )
    return _request(req, portal)


def _text(raw: str) -> str:
    return re.sub(r"\s+", " ", html_mod.unescape(TAG_RE.sub(" ", raw))).strip()


def _site_url(chunk: str, portal_host: str, name: str) -> str:
    for link in LINK_RE.findall(chunk):
        candidate = html_mod.unescape(link).strip()
        host = urllib.parse.urlparse(candidate).netloc.lower()
        if host and portal_host not in host:
            return candidate
    match = BARE_URL_RE.search(_text(chunk))
    if match and portal_host not in urllib.parse.urlparse(match.group(0)).netloc.lower():
        return match.group(0)
    if name and DOMAIN_RE.fullmatch(name):
        return f"http://{name}"
    return ""


def _parse_chunk(chunk: str, portal_host: str) -> dict | None:
    header = HEADER_RE.search(chunk)
    if not header:
        return None
    name = _text(header.group(2))
    server = ""
    found = SERVER_RE.search(chunk) or SERVER_FALLBACK_RE.search(chunk)
    if found:
        server = found.group(1)
    services = []
    for row in SERVICE_ROW_RE.findall(chunk):
        service = SERVICE_NAME_RE.search(row)
        dates = SERVICE_DATE_RE.findall(row)
        placement = PLACEMENT_RE.search(_text(row))
        services.append({
            "srv_id": service.group(1) if service else "",
            "name": _text(service.group(2)) if service else "",
            "start": dates[0] if dates else None,
            "end": dates[1] if len(dates) > 1 else None,
            "placement": placement.group(1).strip() if placement else "",
        })
    groups = []
    for grp in GROUP_RE.finditer(chunk):
        label = GROUP_NAME_RE.search(chunk[grp.end():grp.end() + 400])
        groups.append({
            "id": int(grp.group(1)),
            "name": _text(label.group(1)) if label else "",
        })
    return {
        "id": int(header.group(1)),
        "name": name,
        "url": _site_url(chunk, portal_host, name),
        "server": server,
        "services": services[:10],
        "groups": groups[:10],
    }


def parse_projects(page: str, portal: dict) -> list[dict]:
    cut = page.rfind("</select>")
    region = page[cut + len("</select>"):] if cut >= 0 else page
    end = region.find("</form>")
    if end >= 0:
        region = region[:end]
    portal_host = urllib.parse.urlparse(portal["url"]).netloc.lower()
    headers = list(HEADER_RE.finditer(region))
    merged: dict[int, dict] = {}
    for index, match in enumerate(headers):
        stop = headers[index + 1].start() if index + 1 < len(headers) else len(region)
        found = _parse_chunk(region[match.start():stop], portal_host)
        if found is None:
            continue
        current = merged.get(found["id"])
        if current is None:
            merged[found["id"]] = found
            continue
        if not current["server"] and found["server"]:
            current["server"] = found["server"]
        if not current["url"] and found["url"]:
            current["url"] = found["url"]
        for service in found["services"]:
            if service not in current["services"]:
                current["services"].append(service)
        for group in found["groups"]:
            if group not in current["groups"]:
                current["groups"].append(group)
    return sorted(merged.values(), key=lambda p: p["id"])


def _archive_cache() -> dict:
    try:
        data = json.loads(ARCHIVE_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("flags"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"flags": {}}


def _save_archive_cache(data: dict) -> None:
    ARCHIVE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVE_CACHE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def check_archived(prj_id: int, portal: dict, refresh: bool = False) -> bool | None:
    ttl = float(portal.get("archive_cache_ttl_hours", portal["admin_cache_ttl_hours"]))
    cache = _archive_cache()
    cached = cache["flags"].get(str(prj_id))
    if not refresh and isinstance(cached, dict) and ttl > 0:
        try:
            updated = datetime.strptime(cached["updated"], FMT)
            if (datetime.now() - updated).total_seconds() <= ttl * 3600:
                return cached["archived"]
        except (KeyError, ValueError, TypeError):
            pass
    base = portal["url"].rstrip("/")
    req = urllib.request.Request(
        f"{base}/admin/projects.html"
        f"?subj=EditTable_Form1&fn=edit&prj_name={prj_id}&_prid=1",
        headers=_headers(),
    )
    page = _request(req, portal)
    found = ARCHIVE_RE.search(page)
    if not found:
        return None
    flag = not bool(ARCHIVE_ZERO_RE.search(found.group(1)))
    cache["flags"][str(prj_id)] = {
        "archived": flag, "updated": datetime.now().strftime(FMT),
    }
    _save_archive_cache(cache)
    return flag


def fetch_projects(groups: list[int], portal: dict, refresh: bool = False) -> list[dict]:
    projects = parse_projects(fetch_page(groups, portal), portal)
    if portal.get("check_archive", True):
        for prj in projects:
            prj["archived"] = check_archived(prj["id"], portal, refresh=refresh)
    else:
        for prj in projects:
            prj["archived"] = None
    return projects


def resolve_groups(portal: dict, extended: bool) -> list[int]:
    groups = [int(g) for g in portal["groups"]]
    if extended:
        groups += [int(g) for g in portal["extended_groups"] if int(g) not in groups]
    return groups


def _read_cache(groups: list[int], ttl_hours: float) -> list[dict] | None:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        updated = datetime.strptime(data["updated"], FMT)
        cached_groups = [int(g) for g in data["groups"]]
    except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if sorted(cached_groups) != sorted(groups):
        return None
    if ttl_hours > 0 and (datetime.now() - updated).total_seconds() > ttl_hours * 3600:
        return None
    projects = data.get("projects")
    return projects if isinstance(projects, list) else None


def _write_cache(projects: list[dict], groups: list[int]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated": datetime.now().strftime(FMT),
        "groups": groups,
        "projects": projects,
    }
    CACHE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_projects(portal: dict, extended: bool = False, refresh: bool = False) -> list[dict]:
    groups = resolve_groups(portal, extended)
    if not refresh:
        cached = _read_cache(groups, float(portal["cache_ttl_hours"]))
        if cached is not None:
            return cached
    projects = fetch_projects(groups, portal, refresh=refresh)
    _write_cache(projects, groups)
    return projects


def server_stem(value: str) -> str:
    return value.split(".")[0].strip().lower()


def fetch_admin_table(portal: dict) -> dict:
    base = portal["url"].rstrip("/")
    req = urllib.request.Request(
        f"{base}/admin/projects.html",
        headers={**_headers(), "Cookie": f"pg_count={int(portal['admin_page_size'])}"},
    )
    page = _request(req, portal)
    projects: dict[int, dict] = {}
    for row in ADMIN_ROW_RE.findall(page):
        found = ADMIN_ID_RE.search(row)
        if not found:
            continue
        cells = [_text(cell) for cell in ADMIN_CELL_RE.findall(row)]
        if len(cells) < 5:
            continue
        projects[int(found.group(1))] = {
            "id": int(found.group(1)),
            "name": cells[2],
            "complexity": cells[3],
            "host": cells[4],
            "product": cells[5] if len(cells) > 5 else "",
        }
    payload = {
        "updated": datetime.now().strftime(FMT),
        "page_size": int(portal["admin_page_size"]),
        "truncated": len(projects) >= int(portal["admin_page_size"]),
        "projects": list(projects.values()),
    }
    ADMIN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ADMIN_CACHE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def admin_table(portal: dict, refresh: bool = False) -> dict:
    if not refresh:
        try:
            data = json.loads(ADMIN_CACHE_FILE.read_text(encoding="utf-8"))
            updated = datetime.strptime(data["updated"], FMT)
            ttl = float(portal["admin_cache_ttl_hours"])
            if ttl <= 0 or (datetime.now() - updated).total_seconds() <= ttl * 3600:
                return data
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            pass
    return fetch_admin_table(portal)


def resolve_extended(portal: dict, extended: bool | None = None) -> bool:
    if extended is not None:
        return extended
    return bool(portal.get("extended", True))


def active_projects(
    server: str,
    portal: dict | None = None,
    extended: bool | None = None,
    refresh: bool = False,
) -> list[dict]:
    portal = portal or load_config()
    stem = server_stem(server)
    result: list[dict] = []
    for prj in get_projects(portal, extended=resolve_extended(portal, extended),
                           refresh=refresh):
        if prj.get("archived") is True:
            continue
        if prj.get("archived") is None and portal.get("exclude_archive_unknown"):
            continue
        if prj.get("server") and server_stem(prj["server"]) == stem:
            result.append(prj)
    return sorted(result, key=lambda p: p["id"])


def hosted_projects(
    server: str,
    portal: dict | None = None,
    extended: bool | None = None,
    refresh: bool = False,
) -> list[dict]:
    portal = portal or load_config()
    stem = server_stem(server)
    active = {p["id"]: p for p in active_projects(server, portal, extended, refresh)}
    table = admin_table(portal, refresh=refresh)
    result = [dict(p) for p in active.values()]
    for row in table.get("projects", []):
        prj_id = row["id"]
        if prj_id in active or not row.get("host"):
            continue
        if server_stem(row["host"]) != stem:
            continue
        flag = check_archived(prj_id, portal, refresh=refresh) if portal.get(
            "check_archive", True) else None
        if flag is True or (flag is None and portal.get("exclude_archive_unknown")):
            continue
        result.append({
            "id": prj_id,
            "name": row["name"],
            "url": f"http://{row['name']}" if DOMAIN_RE.fullmatch(row["name"]) else "",
            "server": row["host"],
            "services": [],
            "groups": [],
            "archived": flag,
            "active_service": False,
        })
    return sorted(result, key=lambda p: p["id"])


def projects_for_server(
    server: str,
    portal: dict | None = None,
    extended: bool | None = None,
    refresh: bool = False,
    scope: str | None = None,
) -> list[dict]:
    portal = portal or load_config()
    scope = scope or portal.get("scope", "active")
    if scope == "hosted":
        return hosted_projects(server, portal, extended, refresh)
    return active_projects(server, portal, extended, refresh)


def project_status(prj: dict, portal: dict) -> str:
    strict = {int(g) for g in portal.get("groups") or []}
    extended_ids = {int(g) for g in portal.get("extended_groups") or []}
    gids = {int(g["id"]) for g in prj.get("groups") or []}
    if gids & strict:
        return "поддержка Parts.Resource"
    if gids & extended_ids:
        return "аренда/синхронизатор"
    return "нет активной услуги"


def sort_projects(projects: list[dict], portal: dict) -> list[dict]:
    order = {"поддержка Parts.Resource": 0, "аренда/синхронизатор": 1,
             "нет активной услуги": 2}
    return sorted(projects, key=lambda p: (order[project_status(p, portal)], p["id"]))


def format_block(server: str, projects: list[dict], scope: str | None = None,
                 portal: dict | None = None) -> str:
    portal = portal or load_config()
    scope = scope or portal.get("scope", "active")
    label = "неархивные" if scope == "hosted" else "активные"
    if not projects:
        return f"Затронутые проекты ({server}): {label} не найдено"
    cap = int(portal.get("max_projects_per_server") or 0)
    shown = projects if cap <= 0 else projects[:cap]
    lines = [f"Затронутые проекты ({server}, {label}, {len(projects)}):"]
    for prj in shown:
        name = prj.get("name") or prj.get("url") or str(prj["id"])
        url = prj.get("url") or ""
        suffix = f" — {url}" if url else ""
        status = project_status(prj, portal)
        if scope == "hosted" or status != "поддержка Parts.Resource":
            suffix += f" [{status}]"
        lines.append(f"• {prj['id']} — {name}{suffix}")
    if len(shown) < len(projects):
        lines.append(f"…ещё {len(projects) - len(shown)} — полный список в портале Projects")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Активные проекты сервера по порталу Projects."
    )
    ap.add_argument("--server", help="сервер, например p5ru3 или p5ru3.tradesoft.ru")
    ap.add_argument("--scope", choices=("active", "hosted"),
                    help="active — только с активной услугой Parts.Resource; "
                         "hosted — все неархивные проекты сервера")
    ap.add_argument("--json", action="store_true", help="JSON вместо текстового блока")
    ap.add_argument("--refresh", action="store_true", help="обновить кэш")
    ap.add_argument("--extended", action="store_true",
                    help="учитывать группы 53/74/75 (аренда Parts.Resource, "
                         "синхронизатор, поддержка синхронизатора)")
    ap.add_argument("--no-extended", action="store_true",
                    help="только группы 1/14 — поддержка и лицензия Parts.Resource")
    ap.add_argument("--all", action="store_true", help="все проекты выбранного scope")
    ap.add_argument("--config", help="путь к конфигурации")
    args = ap.parse_args()

    portal = load_config(args.config)
    scope = args.scope or portal.get("scope", "active")
    extended = resolve_extended(
        portal, None if args.extended == args.no_extended else args.extended
    )
    try:
        if args.all:
            if scope == "hosted":
                hosts = sorted({
                    row["host"] for row in admin_table(portal, refresh=args.refresh)
                    .get("projects", []) if row.get("host")
                })
                for host in hosts:
                    print(format_block(
                        host,
                        hosted_projects(host, portal, extended, args.refresh),
                        "hosted", portal,
                    ))
                    print()
                return 0
            projects = get_projects(portal, extended=extended, refresh=args.refresh)
        elif args.server:
            projects = projects_for_server(
                args.server, portal, extended, args.refresh, scope
            )
        else:
            ap.error("нужен --server или --all")
    except RuntimeError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(projects, ensure_ascii=False, indent=2))
        return 0
    if args.all:
        by_server = {}
        for prj in projects:
            if prj.get("archived") is not True:
                label = prj.get("server") or "размещение у Tradesoft (хост не указан)"
                by_server.setdefault(label, []).append(prj)
        for host in sorted(by_server):
            print(format_block(
                host, sort_projects(by_server[host], portal), scope, portal))
            print()
        return 0
    print(format_block(args.server, sort_projects(projects, portal), scope, portal))
    return 0


if __name__ == "__main__":
    sys.exit(main())
