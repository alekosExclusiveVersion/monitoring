#!/usr/bin/env python3
"""tests/test_process_policy.py — политика process() без сети и секретов.

Запуск:  .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tg_support_alert as t

CHAT = -1001

CFG = {
    "incident": {"words": [], "stems": [], "phrases": []},
    "resolved": {
        "enabled": True,
        "words": [],
        "stems": ["восстанов"],
        "phrases": [],
        "problem_phrases": [],
        "problem_words": [],
        "problem_stems": [],
        "cooldown_seconds": 900,
        "include_projects": "auto",
    },
    "planned": {"phrases": [], "stems": [], "override": [], "override_words": []},
    "server_stems": ["p7ru1"],
    "ignore_exact": [],
    "ignore_bots": True,
    "max_message_age_minutes": 60,
    "max_messages_per_run": 5,
    "cooldown_seconds": 300,
    "dedupe_days": 7,
    "chain": {
        "enabled": True,
        "file": "logs/test_chain.json",
        "window_minutes": 720,
        "match_by_project": False,
        "apply_to_incidents": True,
    },
    "shadow": {"enabled": True, "file": "logs/test_shadow.jsonl", "max_lines": 100},
}


def update(text, mid=1):
    return {"update_id": mid, "message": {
        "message_id": mid,
        "chat": {"id": CHAT},
        "from": {"id": 7, "first_name": "Сис. админ", "username": "alxweb_ru"},
        "date": int(datetime.now().timestamp()),
        "text": text,
    }}


class ProcessPolicy(unittest.TestCase):
    def patch(self, name, value):
        original = getattr(t, name)
        setattr(t, name, value)
        self.addCleanup(setattr, t, name, original)

    def patch_prj_config(self):
        original = t.prj.load_config
        t.prj.load_config = lambda: {"scope": "active"}
        self.addCleanup(setattr, t.prj, "load_config", original)

    def test_restore_without_chain_goes_to_shadow(self):
        self.patch_prj_config()
        self.patch("marked_author", lambda msg, text, cfg: True)
        self.patch("is_ignored", lambda text, cfg: False)
        self.patch("log", lambda *args: None)
        sent_calls = []
        shadow_calls = []
        self.patch("send", lambda text, dry_run: sent_calls.append((text, dry_run)))
        self.patch("shadow_log", lambda entry, cfg: shadow_calls.append(entry))

        state = {"messages": {}, "cooldown": {}}
        sent, skipped, shadow = t.process(
            [update("восстановили")], CHAT, CFG, state, {}, False, False)

        self.assertEqual((sent, shadow, skipped), (0, 1, {}))
        self.assertEqual(sent_calls, [])
        self.assertEqual(len(shadow_calls), 1)
        self.assertEqual(shadow_calls[0]["reason"], "restore_noserver")

    def test_dry_run_does_not_write_shadow_file(self):
        tmp = Path(tempfile.mkdtemp(prefix="ts-shadow-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.patch("BASE_DIR", tmp)
        self.patch_prj_config()
        self.patch("marked_author", lambda msg, text, cfg: True)
        self.patch("is_ignored", lambda text, cfg: False)
        self.patch("log", lambda *args: None)

        state = {"messages": {}, "cooldown": {}}
        sent, skipped, shadow = t.process(
            [update("p7ru1")], CHAT, CFG, state, {}, False, True)

        self.assertEqual((sent, shadow, skipped), (0, 1, {}))
        self.assertFalse((tmp / "logs" / "test_shadow.jsonl").exists())

    def test_want_projects_auto(self):
        self.assertTrue(t.want_projects("сервер и проекты онлайн", CFG, False))
        self.assertTrue(t.want_projects("восстановили", CFG, True))
        self.assertFalse(t.want_projects("восстановили", CFG, False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
