#!/usr/bin/env python3
"""tg_support_alert.py — оповещения по сообщениям группы ts-support.

Один опрос getUpdates ботом-читателем: сообщения с ключевыми словами или именем
сервера превращаются в уведомление через notify.notify_all (B24 chat123028 +
Telegram-комната 4385). Для каждого упомянутого сервера добавляется блок
«Затронутые проекты» из портала Projects (prj_active_projects).

Состояние:
  logs/tg_support_reader_state.json — offset getUpdates (tg_support_read);
  logs/tg_support_sent.json         — дедуп по message_id и cooldown;
  logs/tg_support.lock              — защита от параллельного запуска;
  logs/tg_support_alert.log         — журнал запусков.

Режимы:
  (по умолчанию)  разовый опрос и отправка;
  --dry-run        собрать сообщения, напечатать, ничего не отправлять;
  --reset          сбросить offset перед опросом;
  --no-extended    блок проектов только по группам 1/14 (без 53/74/75).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

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
PUNCT_RE = re.compile(r"[^\w\s-]", re.UNICODE)


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


def normalize(text: str) -> str:
    return PUNCT_RE.sub(" ", text.lower()).split()


def is_ignored(text: str, cfg: dict) -> bool:
    flat = " ".join(normalize(text))
    return flat in {" ".join(normalize(p)) for p in cfg.get("ignore_exact", [])}


def detect_servers(text: str, cfg: dict) -> list[str]:
    found: list[tuple[int, str]] = []
    for stem in cfg.get("server_stems", []):
        pattern = rf"(?<![A-Za-z0-9]){re.escape(stem)}(?![A-Za-z0-9])"
        match = re.search(pattern, text, re.I)
        if match:
            found.append((match.start(), match.group(0).lower()))
    found.sort()
    ordered: list[str] = []
    for _, stem in found:
        if stem not in ordered:
            ordered.append(stem)
    return ordered


def find_hits(text: str, cfg: dict) -> list[str]:
    low = text.lower()
    hits = [kw for kw in cfg.get("keywords", []) if kw.lower() in low]
    return hits + detect_servers(text, cfg)


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


def process(updates: list[dict], chat_id: int, cfg: dict, state: dict,
            extended: bool, dry_run: bool) -> int:
    sent = 0
    limit = int(cfg.get("max_messages_per_run", 5))
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
        if not text or is_ignored(text, cfg):
            continue
        hits = find_hits(text, cfg)
        if not hits:
            continue
        msg_id = str(msg.get("message_id") or update["update_id"])
        if msg_id in state["messages"]:
            continue
        servers = detect_servers(text, cfg)
        signature = ",".join(servers) or "-"
        last = state["cooldown"].get(signature)
        cooldown = int(cfg.get("cooldown_seconds", 300))
        now = datetime.now()
        if last and cooldown > 0:
            try:
                if now - datetime.strptime(last, FMT) < timedelta(seconds=cooldown):
                    log(f"пропущено по cooldown ({signature}): {text[:60]}")
                    state["messages"][msg_id] = now.strftime(FMT)
                    continue
            except ValueError:
                pass
        log(f"срабатывание {signature}: {text[:120]}")
        send(build_message(msg, servers, extended), dry_run)
        state["messages"][msg_id] = now.strftime(FMT)
        state["cooldown"][signature] = now.strftime(FMT)
        sent += 1
        if sent >= limit:
            break
    return sent


def main() -> int:
    ap = argparse.ArgumentParser(description="Оповещения по группе ts-support.")
    ap.add_argument("--dry-run", action="store_true", help="не отправлять, только показать")
    ap.add_argument("--reset", action="store_true", help="сбросить offset перед опросом")
    ap.add_argument("--no-extended", action="store_true",
                    help="только группы 1/14 в блоке проектов")
    ap.add_argument("--limit", type=int, default=100, help="апдейтов за опрос")
    args = ap.parse_args()

    cfg = load_config()
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
        sent = process(updates, chat_id, cfg, state, extended, args.dry_run)
        if args.dry_run:
            log("dry-run: offset и дедуп не сохранялись")
        else:
            save_offset(new_offset)
            save_sent(state)
        log(f"апдейтов: {len(updates)}, уведомлений: {sent}, offset: {new_offset}")
    except (RuntimeError, urllib.error.URLError, TimeoutError) as e:
        log(f"Ошибка опроса: {e}")
        return 1
    finally:
        release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
