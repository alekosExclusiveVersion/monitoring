#!/usr/bin/env python3
"""Кроссплатформенные каналы уведомлений: macOS-баннер, B24-чат, Telegram-комната.

Секреты:
  macOS    — Keychain через ~/bin/keychain-get;
  Windows  — Credential Manager через keyring (win_secrets.py);
  любой    — переменная окружения SEC_<UPPER_SNAKE_SERVICE> (для тестов и
             параметризации).
Чтение только из этих источников, никогда из файлов/кода.

Канал macos задействуется только на darwin; на остальных платформах B24 и
Telegram шлются без него.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path

B24_CHAT = os.environ.get("B24_CHAT", "chat123028")
TG_HOST = os.environ.get("TG_HOST", "149.154.167.220")
TS_B24 = Path(
    os.environ.get("TS_B24", str(Path.home() / "Work/ts-b24/scripts"))
)
SECRET_NS = "opencode"


def _env_secret(service: str) -> str | None:
    key = service.removeprefix("opencode.")
    env_key = "SEC_" + key.upper().replace(".", "_").replace("-", "_")
    return os.environ.get(env_key)


def secret_get(service: str) -> str:
    """Секрет по приоритету: env → Credential Manager (Windows) → Keychain."""
    env_val = _env_secret(service)
    if env_val:
        return env_val
    if sys.platform == "darwin":
        return keychain_get(service)
    import keyring

    val = keyring.get_password(SECRET_NS, service)
    if not val:
        raise RuntimeError(
            f"{service}: запись не найдена в Credential Manager; "
            "задайте через win_secrets.py set"
        )
    return val


def keychain_get(service: str) -> str:
    """macOS-источник секретов (основной путь — ~/bin/keychain-get)."""
    res = subprocess.run(
        [str(Path.home() / "bin/keychain-get"), service],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "KEYCHAIN_ASK": "0"},
    )
    if res.returncode != 0:
        raise RuntimeError(f"keychain-get {service}: rc={res.returncode}")
    return res.stdout.strip()


def _esc_osascript(text: str) -> str:
    return __import__("re").sub(r'([\\"])', r"\\\1", str(text))


def send_macos(title: str, body: str) -> None:
    first_line = body.splitlines()[0][:90] if body else title
    script = (
        f'display notification "{_esc_osascript(body)}" '
        f'with title "{_esc_osascript(title)}" subtitle "{_esc_osascript(first_line)}" '
        'sound name "Basso"'
    )
    res = subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        capture_output=True, text=True, timeout=15,
    )
    if res.returncode != 0:
        raise RuntimeError(f"osascript rc={res.returncode}: {res.stderr.strip()[:120]}")


def _b24_client():
    sys.path.insert(0, str(TS_B24))
    from b24_client import B24Client

    if not os.environ.get("B24_BASE_URL"):
        os.environ["B24_BASE_URL"] = secret_get("opencode.ts-b24.base-url")
    if not os.environ.get("B24_WEBHOOK_TOKEN"):
        os.environ["B24_WEBHOOK_TOKEN"] = secret_get("opencode.ts-b24.webhook-token")
    return B24Client(env_file=None)


def send_b24(text: str, dialog: str = B24_CHAT) -> None:
    client = _b24_client()
    client.call("im.message.add", {"DIALOG_ID": dialog, "MESSAGE": text})


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or secret_get("opencode.tg-alert-token")
    chat_id = (
        os.environ.get("TG_ALERT_CHAT_ID2")
        or os.environ.get("TG_ALERT_CHAT_ID")
        or secret_get("opencode.tg-alert-chat2")
    )
    thread_id = None
    try:
        thread_id = secret_get("opencode.tg-alert-thread")
    except RuntimeError:
        pass
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TG_ALERT_CHAT_ID2 not set")
    payload = {"chat_id": chat_id, "text": text}
    if thread_id:
        payload["message_thread_id"] = int(thread_id)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        f"https://{TG_HOST}/bot{token}/sendMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Host": "api.telegram.org"},
    )
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        resp.read()


def notify_all(title: str, text: str) -> dict[str, str]:
    """Отправляет во все каналы; возвращает {канал: статус/ошибка}.

    macOS-баннер задействуется только на darwin; на Windows — B24 и Telegram.
    """
    result = {}
    channels = [("b24", lambda: send_b24(text)),
                ("telegram", lambda: send_telegram(text))]
    if sys.platform == "darwin":
        channels.insert(0, ("macos", lambda: send_macos(title, text)))
    for name, fn in channels:
        try:
            fn()
            result[name] = "ok"
        except Exception as e:
            result[name] = str(e)[:120]
    return result


if __name__ == "__main__":
    text = sys.argv[1] if len(sys.argv) > 1 else "тест уведомления"
    title = sys.argv[2] if len(sys.argv) > 2 else "monitoring"
    for channel, status in notify_all(title, text).items():
        print(f"{channel}: {status}")