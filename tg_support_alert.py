#!/usr/bin/env python3
"""tg_support_alert.py — оповещения по сообщениям группы ts-support.

Один опрос getUpdates ботом-читателем: вид сообщения определяет msg_parse
.classify() — инцидент, восстановление или «в shadow-лог». Уведомления уходят
через notify.notify_all (B24 chat123028 + Telegram-комната 4385). Для каждого
сервера, названного в сообщении или подобранного из цепочки инцидента,
добавляется блок «Затронутые проекты» из портала Projects
(prj_active_projects): в одном уведомлении только эти серверы.

Сообщения не от «своих» авторов уведомлений не дают: подходит username из
author_usernames либо пометка из author_marks (по умолчанию «Сис. админ») в
username, first_name, last_name автора или в начале текста — регистр и точки не
важны. Пустой список снимает фильтр, --any-author отключает его разово.

Уведомление дает только свежие сообщения: старше max_message_age_minutes
(по умолчанию 60) они пропускаются как старые инциденты, --max-age меняет
порог, 0 — без ограничения.

Цепочка инцидента (chain): отправленное уведомление-инцидент открывает сервер
в logs/tg_support_chain.json, поэтому «восстановили» без названия сервера
достраивается до последнего открытого сервера, а «опять лежит» — до него же.
Восстановление закрывает цепочку. Окно chain.window_minutes (720 по умолчанию),
при необходимости сервер уточняется названным доменом проекта
(chain.match_by_project).

Восстановление (resolved) транслируется как ✅ «онлайн»; resolved.include_projects
= auto добавляет блок проектов, если сервер подобран из цепочки или в тексте
есть слова про проекты/сайты. Отдельный cooldown resolved.cooldown_seconds.

Всё, что не распознано, но пришло от доверенного автора, пишется в shadow-лог
(logs/tg_support_unmatched.jsonl) — по нему словари дополняются фактическими
формулировками; --shadow показывает последние записи.

Состояние:
  logs/tg_support_reader_state.json — offset getUpdates (tg_support_read);
  logs/tg_support_sent.json         — дедуп по message_id и cooldown;
  logs/tg_support_chain.json        — открытые инциденты по серверам;
  logs/tg_support.lock              — защита от параллельного запуска;
  logs/tg_support_alert.log         — журнал запусков.

Режимы:
  (по умолчанию)  разовый опрос и отправка;
  --dry-run        собрать сообщения, напечатать, ничего не отправлять;
  --reset          сбросить offset перед опросом;
  --no-extended    блок проектов только по группам 1/14 (без 53/74/75);
  --any-author     не фильтровать по автору (author_marks/author_usernames);
  --max-age N      порог возраста сообщения, мин (0 — без ограничения);
  --shadow [N]     показать последние N записей shadow-лога (по умолчанию 20).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

import msg_parse
import prj_active_projects as prj
from notify import notify_all, secret_get
from tg_support_read import (
    TOKEN_SERVICE,
    api,
    load_offset,
    save_offset,
    target_chat,
)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "tg_support_config.json"
SENT_FILE = BASE_DIR / "logs" / "tg_support_sent.json"
LOCK_FILE = BASE_DIR / "logs" / "tg_support.lock"
LOG_FILE = BASE_DIR / "logs" / "tg_support_alert.log"
FMT = "%Y-%m-%d %H:%M:%S"
MAX_TEXT = 3500
STALE_LOCK_SEC = 3600
PLURAL_HINTS = ("серверы", "сервера", "проекты онлайн", "сайты работают")


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"{CONFIG_FILE.name}: {e}") from e


def log(message: str) -> None:
    stamp = datetime.now().strftime(FMT)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except OSError:
        pass
    print(f"{stamp} {message}", flush=True)


def acquire_lock() -> bool:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        age = datetime.now().timestamp() - LOCK_FILE.stat().st_mtime
        if age < STALE_LOCK_SEC:
            return False
        LOCK_FILE.unlink(missing_ok=True)
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.write(fd, str(datetime.now().timestamp()).encode("ascii"))
    os.close(fd)
    return True


def release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def load_sent() -> dict:
    try:
        data = json.loads(SENT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"messages": {}, "cooldown": {}}
    data.setdefault("messages", {})
    data.setdefault("cooldown", {})
    return data


def save_sent(state: dict) -> None:
    SENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SENT_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def prune_sent(state: dict, days: int) -> None:
    limit = datetime.now() - timedelta(days=days)
    for key in ("messages", "cooldown"):
        for name, stamp in list(state[key].items()):
            try:
                moment = datetime.strptime(stamp, FMT)
            except ValueError:
                state[key].pop(name, None)
                continue
            if moment < limit:
                state[key].pop(name, None)


def load_chain(cfg: dict) -> dict:
    path = BASE_DIR / (cfg.get("chain") or {}).get("file", "logs/tg_support_chain.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"servers": {}}
    data.setdefault("servers", {})
    return data


def save_chain(chain: dict, cfg: dict) -> None:
    path = BASE_DIR / (cfg.get("chain") or {}).get("file", "logs/tg_support_chain.json")
    window = int((cfg.get("chain") or {}).get("window_minutes", 720))
    cutoff = datetime.now() - timedelta(minutes=window)
    for server, entry in list(chain["servers"].items()):
        if entry.get("closed"):
            try:
                if datetime.strptime(entry["closed"], FMT) < cutoff:
                    chain["servers"].pop(server, None)
            except (KeyError, ValueError):
                chain["servers"].pop(server, None)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(chain, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        log(f"цепочка не сохранена: {e}")


def chain_candidates(chain: dict, cfg: dict) -> list[dict]:
    """Открытые инциденты, свежие по окну chain.window_minutes, свежие первыми."""
    window = int((cfg.get("chain") or {}).get("window_minutes", 720))
    cutoff = datetime.now() - timedelta(minutes=window)
    out = []
    for server, entry in chain.get("servers", {}).items():
        if entry.get("closed"):
            continue
        try:
            since = datetime.strptime(entry["since"], FMT)
        except (KeyError, ValueError):
            continue
        if since >= cutoff:
            out.append({"server": server, **entry})
    out.sort(key=lambda item: item["since"], reverse=True)
    return out


def server_by_project(text: str, servers: list[str], portal: dict,
                      extended: bool) -> str | None:
    """Уточнить сервер по названному в тексте домену его проектов."""
    flat = " ".join(msg_parse.tokens(text))
    if not flat:
        return None
    for server in servers:
        try:
            projects = prj.projects_for_server(server, portal, extended=extended)
        except RuntimeError:
            continue
        for project in projects:
            if " ".join(msg_parse.tokens(project["name"])) in flat:
                return server
    return None


def shadow_path(cfg: dict) -> Path:
    return BASE_DIR / (cfg.get("shadow") or {}).get(
        "file", "logs/tg_support_unmatched.jsonl")


def shadow_log(entry: dict, cfg: dict) -> None:
    section = cfg.get("shadow") or {}
    if not section.get("enabled", True):
        return
    path = shadow_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log(f"shadow-лог недоступен: {e}")
        return
    max_lines = int(section.get("max_lines", 5000))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > max_lines:
            path.write_text("\n".join(lines[-max_lines:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def show_shadow(count: int, cfg: dict) -> int:
    path = shadow_path(cfg)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        print(f"shadow-лог недоступен: {e}")
        return 1
    if not lines:
        print("shadow-лог пуст")
        return 0
    for raw in lines[-count:]:
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        print("%s | %-8s | %-18s | %s%s" % (
            entry.get("ts", "?"), entry.get("reason", "?"),
            entry.get("author", "?")[:18], (entry.get("text") or "")[:90],
            (" | серверы: " + ",".join(entry.get("servers") or []))
            if entry.get("servers") else ""))
    print(f"всего записей: {len(lines)}, показано: {min(count, len(lines))}")
    return 0


def is_ignored(text: str, cfg: dict) -> bool:
    flat = " ".join(msg_parse.tokens(text))
    return flat in {" ".join(msg_parse.tokens(p)) for p in cfg.get("ignore_exact", [])}


def message_link(msg: dict) -> str:
    chat = msg.get("chat") or {}
    msg_id = msg.get("message_id")
    if not msg_id:
        return ""
    username = chat.get("username")
    if username:
        return f"https://t.me/{username}/{msg_id}"
    chat_id = str(chat.get("id") or "")
    if chat_id.startswith("-100"):
        return f"https://t.me/c{chat_id[4:]}/{msg_id}"
    return ""


def author_name(msg: dict) -> str:
    who = msg.get("from") or {}
    return (
        who.get("username")
        or " ".join(filter(None, (who.get("first_name"), who.get("last_name"))))
        or str(who.get("id", "?"))
    )


def mark_key(text: str) -> str:
    """Ключ для сравнения пометок: регистр, пробелы и точки не важны."""
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


def marked_author(msg: dict, text: str, cfg: dict) -> bool:
    """Проверка автора: username из author_usernames или пометка author_marks."""
    keys = {mark_key(m) for m in cfg.get("author_marks", []) if m and m.strip()}
    names = {str(u).lstrip("@").lower() for u in cfg.get("author_usernames", []) if u}
    if not keys and not names:
        return True
    who = msg.get("from") or {}
    username = (who.get("username") or "").lower()
    if username and username in names:
        return True
    if not keys:
        return False
    parts = (who.get("username"), who.get("first_name"), who.get("last_name"))
    if any(k in mark_key(p) for p in parts if p for k in keys):
        return True
    head = mark_key(text[:80])
    return any(k in head for k in keys)


def too_old(msg: dict, max_age: int) -> int | None:
    """Возраст сообщения в минутах, если он старше max_age — вернём возраст."""
    stamp = msg.get("date")
    if not stamp or max_age <= 0:
        return None
    age = (datetime.now().timestamp() - stamp) / 60
    return int(age) if age > max_age else None


def build_message(msg: dict, servers: list[str], extended: bool) -> str:
    portal = prj.load_config()
    stamp = datetime.fromtimestamp(msg.get("date", 0)).strftime(FMT)
    title = "ts-support · " + (", ".join(servers) if servers else "инцидент")
    lines = [f"⚠️ {title} · {stamp}", "", f"{author_name(msg)}: {msg.get('text', '')}"]
    for server in servers:
        lines.append("")
        try:
            scope = portal.get("scope", "active")
            projects = prj.sort_projects(
                prj.projects_for_server(server, portal, extended=extended), portal
            )
            lines.append(prj.format_block(server, projects, scope, portal))
        except RuntimeError as e:
            lines.append(f"Затронутые проекты ({server}): список недоступен — {e}")
    link = message_link(msg)
    if link:
        lines += ["", f"Сообщение: {link}"]
    return "\n".join(lines)


def build_resolved(msg: dict, servers: list[str], extended: bool,
                   include_projects: bool) -> str:
    stamp = datetime.fromtimestamp(msg.get("date", 0)).strftime(FMT)
    title = "ts-support · " + (", ".join(servers) if servers else "восстановление")
    lines = [f"✅ {title} · {stamp} · онлайн", "",
             f"{author_name(msg)}: {msg.get('text', '')}"]
    if include_projects:
        portal = prj.load_config()
        for server in servers:
            lines.append("")
            try:
                scope = portal.get("scope", "active")
                projects = prj.sort_projects(
                    prj.projects_for_server(server, portal, extended=extended), portal
                )
                lines.append(prj.format_block(server, projects, scope, portal))
            except RuntimeError as e:
                lines.append(f"Проекты ({server}): список недоступен — {e}")
    link = message_link(msg)
    if link:
        lines += ["", f"Сообщение: {link}"]
    return "\n".join(lines)


def send(text: str, dry_run: bool) -> None:
    if dry_run:
        log("DRY-RUN:\n" + text)
        return
    for index in range(0, len(text), MAX_TEXT):
        chunk = text[index:index + MAX_TEXT]
        if index:
            chunk = f"(продолжение {index // MAX_TEXT + 1})\n{chunk}"
        for channel, status in notify_all("ts-support", chunk).items():
            log(f"  {channel}: {status}")


def fetch_updates(token: str, offset: int, limit: int) -> tuple[list[dict], int]:
    result = api(
        token,
        "getUpdates",
        offset=offset,
        limit=limit,
        timeout=0,
        allowed_updates='["message","edited_message"]',
    )
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "getUpdates failed"))
    updates = result.get("result") or []
    new_offset = max((u["update_id"] for u in updates), default=offset - 1) + 1
    return updates, new_offset


def want_projects(text: str, cfg: dict, inferred: bool) -> bool:
    """include_projects: true / false / auto — блок проектов в сообщении о
    восстановлении. В auto блок есть, если сервер подобран из цепочки или в
    тексте прямо сказано про проекты/сайты («сервер и проекты онлайн»)."""
    setting = (cfg.get("resolved") or {}).get("include_projects", "auto")
    if isinstance(setting, bool):
        return setting
    if inferred:
        return True
    return bool(msg_parse.match_phrases(
        text, ("проект", "проекты", "сайт", "сайты", "магазин", "магазины")))


def process(updates: list[dict], chat_id: int, cfg: dict, state: dict,
            chain: dict, extended: bool, dry_run: bool, any_author: bool = False,
            max_age: int | None = None) -> tuple[int, dict, int]:
    sent = 0
    skipped: dict[str, int] = {}
    stale = 0
    shadow = 0
    restored = 0
    portal = prj.load_config()
    limit = int(cfg.get("max_messages_per_run", 5))
    age_limit = int(cfg.get("max_message_age_minutes", 60) if max_age is None else max_age)
    resolved_cfg = cfg.get("resolved") or {}
    resolved_enabled = resolved_cfg.get("enabled", True)
    resolved_cooldown = int(resolved_cfg.get("cooldown_seconds", 900))
    chain_cfg = cfg.get("chain") or {}
    chain_enabled = bool(chain_cfg.get("enabled", True))
    match_by_project = bool(chain_cfg.get("match_by_project", True))
    chain_to_incidents = bool(chain_cfg.get("apply_to_incidents", True))
    for update in updates:
        kind = next((k for k in update if k != "update_id"), "")
        if kind not in {"message", "edited_message"}:
            continue
        msg = update[kind]
        if (msg.get("chat") or {}).get("id") != chat_id:
            continue
        if cfg.get("ignore_bots", True) and (msg.get("from") or {}).get("is_bot"):
            continue
        text = msg.get("text") or msg.get("caption") or ""
        if not text:
            continue
        age = too_old(msg, age_limit)
        if age is not None:
            stale += 1
            continue
        verdict = msg_parse.classify(text, cfg)
        if verdict["kind"] in ("restore", "restore_noserver") and not resolved_enabled:
            verdict = {"kind": "shadow", "hits": verdict["hits"],
                       "servers": verdict["servers"], "reason": "resolved_disabled"}
        trusted = any_author or marked_author(msg, text, cfg)
        if not trusted:
            if verdict["kind"] not in ("shadow",):
                name = author_name(msg)
                skipped[name] = skipped.get(name, 0) + 1
            continue
        if is_ignored(text, cfg):
            continue
        msg_id = str(msg.get("message_id") or update["update_id"])
        if msg_id in state["messages"]:
            continue
        kind_out = verdict["kind"]
        servers = list(verdict["servers"])
        inferred = False
        if kind_out in ("restore_noserver", "incident") and not servers and chain_enabled:
            if chain_to_incidents or kind_out == "restore_noserver":
                open_servers = chain_candidates(chain, cfg)
                if open_servers:
                    names = [item["server"] for item in open_servers]
                    plural = bool(msg_parse.match_phrases(text, PLURAL_HINTS))
                    if match_by_project:
                        found = server_by_project(text, names, portal, extended)
                        if found:
                            servers = [found]
                            log(f"сервер уточнён по домену проекта: {found}")
                    if not servers:
                        servers = names if plural else [names[0]]
                        if len(servers) > 1:
                            log(f"по цепочке подставлено серверов: {len(servers)} "
                                f"({', '.join(servers)})")
                    inferred = True
                    if kind_out == "restore_noserver":
                        kind_out = "restore"
        if kind_out == "shadow":
            shadow_log({
                "ts": datetime.now().strftime(FMT),
                "author": author_name(msg),
                "username": (msg.get("from") or {}).get("username"),
                "text": text[:500],
                "servers": servers,
                "reason": verdict["reason"],
                "hits": verdict["hits"][:10],
                "link": message_link(msg),
            }, cfg)
            shadow += 1
            continue
        signature = ",".join(servers) or "-"
        prefix = "back:" if kind_out == "restore" else ""
        last = state["cooldown"].get(prefix + signature)
        cooldown = resolved_cooldown if kind_out == "restore" else int(
            cfg.get("cooldown_seconds", 300))
        now = datetime.now()
        if last and cooldown > 0:
            try:
                if now - datetime.strptime(last, FMT) < timedelta(seconds=cooldown):
                    log(f"пропущено по cooldown ({prefix or 'alert:'} {signature}): {text[:60]}")
                    state["messages"][msg_id] = now.strftime(FMT)
                    continue
            except ValueError:
                pass
        if kind_out == "restore":
            log(f"восстановление {signature} ({', '.join(verdict['hits'])}): {text[:120]}")
            send(build_resolved(msg, servers, extended,
                                want_projects(text, cfg, inferred)), dry_run)
            for server in servers:
                if server in chain.get("servers", {}):
                    chain["servers"][server]["closed"] = now.strftime(FMT)
            restored += 1
        else:
            tag = " (сервер из цепочки)" if inferred else ""
            log(f"срабатывание {signature}{tag}: {text[:120]}")
            send(build_message(msg, servers, extended), dry_run)
            for server in servers:
                entry = chain.setdefault("servers", {}).setdefault(server, {})
                entry["since"] = now.strftime(FMT)
                entry["message_id"] = msg_id
                entry["text"] = text[:200]
                entry.pop("closed", None)
        state["messages"][msg_id] = now.strftime(FMT)
        state["cooldown"][prefix + signature] = now.strftime(FMT)
        sent += 1
        if sent >= limit:
            break
    if stale:
        log(f"пропущено: старше {age_limit} мин — {stale}")
    if shadow:
        log(f"shadow-лог: {shadow} нераспознанных сообщений")
    log(f"уведомлений: {sent} (из них о восстановлении: {restored})")
    return sent, skipped, shadow


def main() -> int:
    ap = argparse.ArgumentParser(description="Оповещения по группе ts-support.")
    ap.add_argument("--dry-run", action="store_true", help="не отправлять, только показать")
    ap.add_argument("--reset", action="store_true", help="сбросить offset перед опросом")
    ap.add_argument("--no-extended", action="store_true",
                    help="только группы 1/14 в блоке проектов")
    ap.add_argument("--any-author", action="store_true",
                    help="не фильтровать по автору (author_marks/author_usernames)")
    ap.add_argument("--max-age", type=int, default=None,
                    help="максимальный возраст сообщения, мин (0 — без ограничения)")
    ap.add_argument("--shadow", nargs="?", const=20, type=int, default=None,
                    metavar="N", help="показать последние N записей shadow-лога")
    ap.add_argument("--limit", type=int, default=100, help="апдейтов за опрос")
    args = ap.parse_args()

    cfg = load_config()
    if args.shadow is not None:
        return show_shadow(args.shadow, cfg)
    extended = prj.resolve_extended(prj.load_config(), None if not args.no_extended else False)
    if not acquire_lock():
        log("пропуск: другой запуск уже работает")
        return 0
    try:
        token = secret_get(TOKEN_SERVICE)
        me = api(token, "getMe")["result"]
        if not me.get("can_read_all_group_messages"):
            log("ВНИМАНИЕ: у бота-читателя включён privacy-режим")
        chat_id = target_chat(token)
        offset = 0 if args.reset else load_offset()
        updates, new_offset = fetch_updates(token, offset, args.limit)
        state = load_sent()
        prune_sent(state, int(cfg.get("dedupe_days", 7)))
        chain = load_chain(cfg)
        sent, skipped, shadow = process(updates, chat_id, cfg, state, chain, extended,
                                        args.dry_run, args.any_author, args.max_age)
        if skipped:
            log("пропущено без пометки автора: %d (%s)" % (
                sum(skipped.values()),
                ", ".join("%s×%d" % (name, count) for name, count in sorted(skipped.items()))))
        if args.dry_run:
            log("dry-run: offset, дедуп и цепочка не сохранялись")
        else:
            save_offset(new_offset)
            save_sent(state)
            save_chain(chain, cfg)
        log(f"апдейтов: {len(updates)}, уведомлений: {sent}, offset: {new_offset}")
    except (RuntimeError, urllib.error.URLError, TimeoutError) as e:
        log(f"Ошибка опроса: {e}")
        return 1
    finally:
        release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
