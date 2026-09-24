#!/usr/bin/env python3
"""win_secrets.py — работа с секретами мониторинга в Windows Credential Manager.

Использует keyring (CRED-хранилище Windows). Service name в keyring = "opencode",
username = ключ из реестра (опенкод.тг-алерт-токен и т.п.).

Команды:
  set <service>          — записать секрет (значение читается из stdin или getpass)
  get <service>          — вывести значение (осторожно: попадает в stdout)
  check <service>        — rc=0 если запись есть (значение не печатается)
  delete <service>       — удалить запись
  list                   — список записей с префиксом opencode.*

Пример (на Windows-машине):
  echo "t0ken" | py win_secrets.py set opencode.tg-alert-token
  py win_secrets.py check opencode.tg-alert-token
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

try:
    import keyring
except ImportError as e:
    sys.stderr.write(
        f"keyring не установлен: {e}\n"
        "Установите зависимость: pip install keyring\n"
    )
    sys.exit(2)

SERVICE = "opencode"
PREFIX = "opencode."


def run() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["set", "get", "check", "delete", "list"])
    ap.add_argument("service", nargs="?", default="")
    args = ap.parse_args()

    if args.cmd == "list":
        creds = getattr(keyring, "get_all_credentials", lambda s: [])(SERVICE)
        if not creds:
            sys.stderr.write("(keyring не поддерживает перечисление записей)\n")
            return 0
        for c in creds:
            if getattr(c, "username", "").startswith(PREFIX):
                print(c.username)
        return 0

    service = args.service
    if not service.startswith(PREFIX):
        sys.stderr.write(f"service должен начинаться с {PREFIX!r}\n")
        return 2

    if args.cmd == "set":
        if sys.stdin.isatty():
            value = getpass.getpass(f"Секрет для {service}: ")
        else:
            value = sys.stdin.read().strip()
        if not value:
            sys.stderr.write("пустое значение, запись не произведена\n")
            return 1
        keyring.set_password(SERVICE, service, value)
        print(f"ok: {service} записан")
        return 0

    if args.cmd == "get":
        value = keyring.get_password(SERVICE, service)
        if value is None:
            sys.stderr.write(f"нет записи {service}\n")
            return 1
        sys.stdout.write(value + "\n")
        return 0

    if args.cmd == "check":
        value = keyring.get_password(SERVICE, service)
        if value is None:
            sys.stderr.write(f"нет записи {service}\n")
            return 1
        print(f"ok: {service}")
        return 0

    if args.cmd == "delete":
        try:
            keyring.delete_password(SERVICE, service)
        except keyring.errors.PasswordDeleteError:
            pass
        print(f"ok: {service} удалён")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(run())