"""
tg_support_read.py

Чтение сообщений Telegram-группы поддержки ботом алертов через getUpdates.

Секреты (приоритет notify.secret_get: env → Credential Manager → Keychain):
  opencode.tg-support-reader-token — токен бота-читателя;
  opencode.tg-support-chat         — chat_id группы (env: TG_SUPPORT_CHAT_ID).

Режимы:
  по умолчанию  — показать непрочитанные сообщения и выйти;
  --follow      — long-poll, печать новых сообщений до Ctrl+C;
  --reset       — сбросить offset (показать всю доступную очередь заново).

Offset хранится в logs/tg_support_reader_state.json. Апдейты других чатов
пропускаются, но учитываются в offset. Бот-читатель только читает: уведомления
отправляет tg_support_alert.py через notify.py (отдельный alert-бот).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from notify import secret_get

TOKEN_SERVICE = "opencode.tg-support-reader-token"
CHAT_SERVICE = "opencode.tg-support-chat"
API_BASE = "https://api.telegram.org"
STATE_FILE = Path(
    os.environ.get(
        "TG_SUPPORT_STATE",
        str(Path(__file__).resolve().parent / "logs" / "tg_support_reader_state.json"),
    )
)
HTTP_TIMEOUT = 60
FMT = "%Y-%m-%d %H:%M:%S"


def api(token: str, method: str, **params) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}/bot{token}/{method}",
        data=urllib.parse.urlencode(params, doseq=True).encode("utf-8"),
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def target_chat(token: str) -> int:
    raw = os.environ.get("TG_SUPPORT_CHAT_ID") or secret_get(CHAT_SERVICE)
    chat = api(token, "getChat", chat_id=raw)["result"]
    print(f"Чат: {chat.get('title')} ({chat.get('type')}, id={chat.get('id')})")
    return int(chat["id"])


def load_offset() -> int:
    try:
        return int(json.loads(STATE_FILE.read_text(encoding="utf-8"))["offset"])
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return 0


def save_offset(offset: int) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps({"offset": offset, "updated": datetime.now().strftime(FMT)},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def format_update(update: dict) -> str | None:
    kind = next((k for k in update if k != "update_id"), "")
    if kind not in {"message", "edited_message"}:
        return None
    msg = update[kind]
    who = msg.get("from") or {}
    author = who.get("username") or " ".join(
        filter(None, (who.get("first_name"), who.get("last_name")))) or str(who.get("id"))
    text = msg.get("text") or msg.get("caption") or ""
    if not text:
        if msg.get("new_chat_members"):
            names = ", ".join(
                u.get("username") or str(u.get("id")) for u in msg["new_chat_members"])
            text = f"[в группе добавлен: {names}]"
        elif msg.get("left_chat_member"):
            left = msg["left_chat_member"]
            text = f"[покинул группу: {left.get('username') or left.get('id')}]"
        else:
            kinds = [k for k in msg if k not in {"message_id", "date", "chat", "from"}]
            text = f"[без текста: {', '.join(kinds) or '—'}]"
    stamp = datetime.fromtimestamp(msg.get("date", 0)).strftime(FMT)
    prefix = "изм." if kind == "edited_message" else "    "
    return f"{stamp} {prefix} {author}: {text}"


def poll(token: str, chat_id: int, offset: int, limit: int,
         timeout: int) -> tuple[list[str], int]:
    result = api(token, "getUpdates", offset=offset, limit=limit, timeout=timeout,
                 allowed_updates='["message","edited_message"]')
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "getUpdates failed"))
    updates = result.get("result") or []
    lines = []
    for update in updates:
        kind = next((k for k in update if k != "update_id"), "")
        if (update.get(kind) or {}).get("chat", {}).get("id") != chat_id:
            continue
        line = format_update(update)
        if line:
            lines.append(line)
    new_offset = max((u["update_id"] for u in updates), default=offset - 1) + 1
    return lines, new_offset


def main() -> int:
    parser = argparse.ArgumentParser(description="Чтение сообщений группы ts-support.")
    parser.add_argument("--follow", action="store_true", help="long-poll до Ctrl+C")
    parser.add_argument("--reset", action="store_true", help="сбросить offset перед чтением")
    parser.add_argument("--limit", type=int, default=100, help="макс. апдейтов за опрос")
    parser.add_argument("--timeout", type=int, default=None,
                        help="long-poll ожидание, сек (по умолчанию 30 в --follow, иначе 0)")
    args = parser.parse_args()
    timeout = args.timeout if args.timeout is not None else (30 if args.follow else 0)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    token = secret_get(TOKEN_SERVICE)
    me = api(token, "getMe")["result"]
    if not me.get("can_read_all_group_messages"):
        print("ВНИМАНИЕ: privacy-режим бота включён, бот не увидит обычные сообщения",
              file=sys.stderr)
    chat_id = target_chat(token)
    offset = 0 if args.reset else load_offset()

    while True:
        try:
            lines, offset = poll(token, chat_id, offset, args.limit, timeout)
            for line in lines:
                print(line, flush=True)
            save_offset(offset)
        except (urllib.error.URLError, RuntimeError, TimeoutError) as e:
            print(f"Ошибка опроса: {e}", file=sys.stderr)
            if not args.follow:
                return 1
        if not args.follow:
            return 0
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
