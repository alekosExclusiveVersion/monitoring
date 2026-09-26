#!/usr/bin/env python3
"""msg_parse.py — разбор сообщений ts-support: словари, серверы, classify().

Модуль чистый: без сети, файлов и секретов, поэтому его легко проверять тестами
(tests/test_msg_parse.py). Три словаря на сообщение:

  words   — точный токен целиком («диск», «500»); «4500» и «1500 отказов» не ловятся;
  stems   — префикс токена («тормоз» ловит «тормозит»);
  phrases — несколько слов подряд с границами («не работает»).

Порядок решений задан в classify() и одинаков для инцидента, восстановления и
«в shadow-лог»; приоритет проблемы выше признака восстановления, а сервер
может быть назван явно или подобран вызывающим кодом из цепочки инцидента.
"""

from __future__ import annotations

import re
from typing import Iterable

HOMOGLYPHS = str.maketrans({
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o",
    "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "і": "i", "ѕ": "s",
    "ј": "j", "ԁ": "d", "һ": "h", "ӏ": "l",
})
PUNCT_RE = re.compile(r"[^\w\s-]", re.UNICODE)
DASH_RE = re.compile(r"[‐‑‒–—―]+", re.UNICODE)
SPACE_RE = re.compile(r"\s+", re.UNICODE)
SEP_RE = r"[\s._-]*"
_SERVER_PATTERNS: dict[str, re.Pattern] = {}


def normalize(text: str) -> str:
    """Нижний регистр, ё→е, любые тире → «-», пунктуация → пробелы."""
    low = str(text).lower().replace("ё", "е")
    low = DASH_RE.sub("-", low)
    low = PUNCT_RE.sub(" ", low)
    return SPACE_RE.sub(" ", low).strip()


def fold(text: str) -> str:
    """Кириллические омоглифы → латиница; только для поиска имён серверов."""
    return str(text).lower().translate(HOMOGLYPHS)


def tokens(text: str) -> list[str]:
    return normalize(text).split()


def _unique(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def match_words(text: str, words: Iterable[str]) -> list[str]:
    """Точное совпадение слова или числа как отдельного токена."""
    toks = set(tokens(text))
    return _unique(w for w in words if normalize(w) in toks)


def match_stems(text: str, stems: Iterable[str]) -> list[str]:
    """Токен начинается с основы: «тормоз» ловит «тормозит»."""
    toks = tokens(text)
    return _unique(s for s in stems if any(t.startswith(normalize(s)) for t in toks))


def match_phrases(text: str, phrases: Iterable[str]) -> list[str]:
    """Несколько слов подряд с границами по краям."""
    flat = normalize(text)
    out = []
    for phrase in phrases:
        norm = normalize(phrase)
        if norm and re.search(rf"(?<!\w){re.escape(norm)}(?!\w)", flat):
            out.append(phrase)
    return _unique(out)


def server_pattern(stem: str) -> re.Pattern:
    """p7ru1 → p[.-]?7[.-]?r[.-]?u[.-]?1 с границами по буквам и цифрам."""
    cached = _SERVER_PATTERNS.get(stem)
    if cached is not None:
        return cached
    body = SEP_RE.join(re.escape(ch) for ch in normalize(stem))
    pattern = re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])", re.I)
    _SERVER_PATTERNS[stem] = pattern
    return pattern


def detect_servers(text: str, stems: Iterable[str]) -> list[str]:
    """Серверы из текста: регистр, разделители («P7RU 3»), кириллические
    омоглифы («р7ru1») и хост целиком («p7ru1.auto-vision.ru»)."""
    folded = normalize(fold(text))
    found: list[tuple[int, str]] = []
    for stem in stems:
        match = server_pattern(stem).search(folded)
        if match:
            found.append((match.start(), stem.lower()))
    found.sort()
    return _unique(stem for _, stem in found)


def classify(text: str, cfg: dict) -> dict:
    """Вид сообщения: incident / restore / restore_noserver / shadow.

    Возвращает kind, hits (что сматчилось), servers (названы явно) и reason.
    Подстановка сервера из цепочки инцидента делает вызывающий код — здесь её
    нет, поэтому restore без сервера помечается restore_noserver.
    """
    incident = cfg.get("incident") or {}
    resolved = cfg.get("resolved") or {}
    planned = cfg.get("planned") or {}
    servers = detect_servers(text, cfg.get("server_stems", []))

    problem = _unique(
        match_phrases(text, resolved.get("problem_phrases", []))
        + match_words(text, resolved.get("problem_words", []))
        + match_stems(text, resolved.get("problem_stems", []))
    )
    if problem:
        return {"kind": "incident", "hits": problem, "servers": servers,
                "reason": "problem"}

    back = _unique(
        match_words(text, resolved.get("words", []))
        + match_stems(text, resolved.get("stems", []))
        + match_phrases(text, resolved.get("phrases", []))
    )
    inc = _unique(
        match_words(text, incident.get("words", []))
        + match_stems(text, incident.get("stems", []))
        + match_phrases(text, incident.get("phrases", []))
    )
    plan = _unique(
        match_phrases(text, planned.get("phrases", []))
        + match_stems(text, planned.get("stems", []))
    )
    override = _unique(
        match_phrases(text, planned.get("override", []))
        + match_words(text, planned.get("override_words", []))
    )

    if back:
        kind = "restore" if servers else "restore_noserver"
        reason = "restore" if servers else "restore_noserver"
        return {"kind": kind, "hits": back, "servers": servers, "reason": reason}
    if inc:
        if plan and not override:
            return {"kind": "shadow", "hits": plan, "servers": servers,
                    "reason": "planned"}
        return {"kind": "incident", "hits": inc, "servers": servers,
                "reason": "incident"}
    if servers:
        return {"kind": "shadow", "hits": [], "servers": servers,
                "reason": "server_only"}
    if plan:
        return {"kind": "shadow", "hits": plan, "servers": [], "reason": "planned"}
    return {"kind": "shadow", "hits": [], "servers": [], "reason": "no_hits"}
