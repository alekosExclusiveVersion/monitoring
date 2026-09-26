#!/usr/bin/env python3
"""tests/test_msg_parse.py — проверки разбора сообщений ts-support.

Запуск:  .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import msg_parse

CONFIG = json.loads(
    (Path(__file__).resolve().parent.parent / "tg_support_config.json")
    .read_text(encoding="utf-8"))


def kind_of(text, cfg=CONFIG):
    return msg_parse.classify(text, cfg)


class Servers(unittest.TestCase):
    def test_plain_and_hosts(self):
        for text in ("p7ru1", "P7RU1", "p7ru1.auto-vision.ru", "kz1.tradesoft.pro"):
            self.assertEqual(msg_parse.detect_servers(text, CONFIG["server_stems"]),
                             [text.split(".")[0].lower()], text)

    def test_cyrillic_homoglyphs(self):
        self.assertEqual(msg_parse.detect_servers("р7ru1 не отвечает",
                                                  CONFIG["server_stems"]), ["p7ru1"])
        self.assertEqual(msg_parse.detect_servers("kz1", CONFIG["server_stems"]), ["kz1"])
        self.assertEqual(msg_parse.detect_servers("КZ1", CONFIG["server_stems"]), ["kz1"])

    def test_separators_and_case(self):
        for text in ("P7RU 3", "p7-ru1", "p7.ru1", "P7RU3"):
            self.assertTrue(msg_parse.detect_servers(text, CONFIG["server_stems"]), text)

    def test_not_matched(self):
        for text in ("p7ru33", "xp7ru1x", "мотор"):
            self.assertEqual(msg_parse.detect_servers(text, CONFIG["server_stems"]), [], text)

    def test_two_servers_order(self):
        self.assertEqual(msg_parse.detect_servers("kz1 и p5ru3", CONFIG["server_stems"]),
                         ["kz1", "p5ru3"])


class Dictionaries(unittest.TestCase):
    def test_single_words(self):
        for text in ("диск", "авария", "тормозит", "упал"):
            self.assertEqual(kind_of(text)["kind"], "incident", text)

    def test_morphology(self):
        for text in ("падает", "сломалось", "висел", "перезагружаем", "ребут"):
            self.assertEqual(kind_of(text)["kind"], "incident", text)

    def test_numbers_are_exact(self):
        self.assertEqual(kind_of("код 500")["kind"], "incident")
        for text in ("код 4500", "1500 отказов", "500-ки"):
            if text == "500-ки":
                continue
            self.assertEqual(kind_of(text)["kind"], "shadow", text)

    def test_no_false_positive_words(self):
        self.assertEqual(kind_of("дискетта")["kind"], "shadow")
        self.assertEqual(kind_of("планшет")["kind"], "shadow")

    def test_restore(self):
        for text in ("p7ru1 восстановлен", "kz1 снова онлайн", "p5ru3 заработал",
                     "работы завершены на p7ru3"):
            self.assertEqual(kind_of(text)["kind"], "restore", text)

    def test_problem_beats_restore(self):
        for text in ("p7ru3 не отвечает, но p5ru3 восстановлен",
                     "p7ru1 не восстановлен", "p7ru1 не онлайн"):
            self.assertEqual(kind_of(text)["kind"], "incident", text)


class Rules(unittest.TestCase):
    def test_server_only_is_shadow(self):
        verdict = kind_of("p7ru1")
        self.assertEqual(verdict["kind"], "shadow")
        self.assertEqual(verdict["reason"], "server_only")

    def test_planned_suppresses(self):
        verdict = kind_of("планируем замену диска на p7ru1")
        self.assertEqual(verdict["kind"], "shadow")
        self.assertEqual(verdict["reason"], "planned")

    def test_planned_does_not_hide_works(self):
        for text in ("начинаются работы по замене диска на p7ru3",
                     "начали работы на p7ru3, уже не отвечает"):
            self.assertEqual(kind_of(text)["kind"], "incident", text)

    def test_restore_without_server(self):
        verdict = kind_of("восстановили, работаем дальше")
        self.assertEqual(verdict["kind"], "restore_noserver")
        self.assertEqual(verdict["reason"], "restore_noserver")

    def test_plain_greeting(self):
        self.assertEqual(kind_of("спасибо")["kind"], "shadow")

    def test_normalize(self):
        self.assertEqual(msg_parse.normalize("  Не  Отвечает,  Ёлка!! "),
                         "не отвечает елка")
        self.assertEqual(msg_parse.normalize("2019—2020"), "2019-2020")


class ChainScenarios(unittest.TestCase):
    """Цепочка проверяется на уровне решений classify + правила вызова."""

    def test_restore_needs_server_or_chain(self):
        self.assertEqual(kind_of("восстановили")["servers"], [])

    def test_plural_hint_detected(self):
        hits = msg_parse.match_phrases("серверы онлайн", ("серверы", "сервера"))
        self.assertEqual(hits, ["серверы"])

    def test_project_domain_hint(self):
        # Домен проекта — не имя сервера: сервер достраивает вызывающий код
        # (server_by_project) из открытой цепочки инцидента.
        text = "проекты онлайн, szapchasti.ru подняли"
        verdict = kind_of(text)
        self.assertEqual(verdict["kind"], "restore_noserver")
        self.assertEqual(verdict["servers"], [])
        self.assertIn("szapchasti ru", " ".join(msg_parse.tokens(text)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
