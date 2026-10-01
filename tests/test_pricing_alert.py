#!/usr/bin/env python3
"""tests/test_pricing_alert.py — тихий ярус одиночных сбоев проценки.

Запуск:  .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pricing_alert as pa

TF, TT = 1790420632, 1790424232


def make_error(errors=770, total=1000):
    return {"e": errors, "n": total}


def make_runtime(avg, total=1000):
    return {"avg_r": avg, "p95": avg * 2.0, "n": total}


def make_window(*, runtime=None, errors=None):
    return {
        "runtime": dict(runtime or {}),
        "errors": dict(errors or {}),
        "volume": {"n": 1000, "slow": 0, "avg_t": 1.0, "p99": 5.0},
    }


def full_config(**overrides):
    cfg = pa._load_config()
    cfg.update(overrides)
    return cfg


def read_decisions():
    return [
        json.loads(line)["decision"]
        for line in pa.EVENTS_LOG.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def read_state():
    return json.loads(pa.STATE.read_text(encoding="utf-8"))


class PricingAlertTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pricing-alert-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sent = []

        self._original_state = pa.STATE
        self._original_events = pa.EVENTS_LOG
        pa.STATE = self.tmp / "pricing_alert_state.json"
        pa.EVENTS_LOG = self.tmp / "pricing_alert_events.jsonl"
        self.addCleanup(setattr, pa, "STATE", self._original_state)
        self.addCleanup(setattr, pa, "EVENTS_LOG", self._original_events)

        notify = mock.patch.object(
            pa,
            "_notify",
            side_effect=lambda text, title: self.sent.append((title, text)),
        )
        notify.start()
        self.addCleanup(notify.stop)

    def run_main_hourly(self, cur, base, cfg, watchdog_n=1000):
        """Без сброса метки: второй прогон в том же часе идёт в watchdog."""
        with (
            mock.patch.object(pa, "_window_cur", return_value=cur) as wcur,
            mock.patch.object(pa, "_base_for", return_value=base),
            mock.patch.object(pa, "_watchdog_counts",
                              return_value={"n": watchdog_n, "avg_t": 1.0}),
            mock.patch.object(pa, "_refresh_registry_if_due",
                              side_effect=lambda reg, cfg, now_s: reg),
            mock.patch.object(pa, "_load_config", return_value=cfg),
            mock.patch.object(pa, "_runs_phase2", return_value=([], 0.0)),
            mock.patch.object(pa, "_attach_trends", return_value=None),
        ):
            rc = pa.main()
            return rc, wcur.call_count


class ConfigDefaults(PricingAlertTestCase):
    def test_empty_config_file_receives_every_default(self):
        config_path = self.tmp / "empty-pricing-config.json"
        config_path.write_text("{}", encoding="utf-8")

        with mock.patch.object(pa, "CONFIG", config_path):
            loaded = pa._load_config()

        self.assertEqual(loaded, pa.CONFIG_DEFAULTS)


class GlobalDetection(PricingAlertTestCase):
    def test_three_runtime_providers_stay_global(self):
        cfg = full_config()
        cur = make_window(runtime={name: make_runtime(20.0) for name in ("a", "b", "c")})
        base = make_window(runtime={name: make_runtime(5.0) for name in ("a", "b", "c")})
        det = pa._detect(cur, base, cfg)

        self.assertEqual(len(det["runtime"]), 3)
        self.assertEqual(det["suppressed"], {})

    def test_three_error_providers_stay_global(self):
        cfg = full_config()
        cur = make_window(errors={name: make_error(400) for name in ("a", "b", "c")})
        base = make_window(errors={name: make_error(20) for name in ("a", "b", "c")})
        det = pa._detect(cur, base, cfg)

        self.assertEqual(len(det["errors"]), 3)
        self.assertEqual(det["suppressed"], {})


class SingleSeverity(PricingAlertTestCase):
    def test_suppressed_single_error_keeps_severity(self):
        cfg = full_config()
        det = pa._detect(
            make_window(errors={"akparts": make_error(770)}),
            make_window(errors={"akparts": make_error(244)}),
            cfg,
        )

        self.assertEqual(det["errors"], [])
        entry = det["suppressed"]["errors"]["entries"][0]
        self.assertEqual(det["suppressed"]["errors"]["providers"], ["akparts"])
        self.assertEqual(entry["pct"], 77.0)
        self.assertEqual(entry["base_pct"], 24.4)

    def test_error_threshold_comes_from_config(self):
        det = pa._detect(
            make_window(errors={"akparts": make_error(770)}),
            make_window(errors={"akparts": make_error(244)}),
            full_config(),
        )

        passing = full_config(single_provider_error_pct=50.0)
        blocking = full_config(single_provider_error_pct=90.0)
        self.assertEqual(pa._single_hits(det, passing), ["akparts"])
        self.assertEqual(pa._single_hits(det, blocking), [])

    def test_runtime_threshold_comes_from_config(self):
        det = pa._detect(
            make_window(runtime={"carsdam_ru": make_runtime(21.0)}),
            make_window(runtime={"carsdam_ru": make_runtime(0.3)}),
            full_config(),
        )

        passing = full_config(single_provider_runtime_sec=20.0)
        blocking = full_config(single_provider_runtime_sec=25.0)
        self.assertEqual(pa._single_hits(det, passing), ["carsdam_ru"])
        self.assertEqual(pa._single_hits(det, blocking), [])

    def test_two_severe_providers_are_visible_but_over_default_limit(self):
        cfg = full_config()
        det = pa._detect(
            make_window(runtime={"p1": make_runtime(25.0), "p2": make_runtime(26.0)}),
            make_window(runtime={"p1": make_runtime(5.0), "p2": make_runtime(5.0)}),
            cfg,
        )

        self.assertEqual(det["suppressed"]["runtime"]["n"], 2)
        self.assertEqual(sorted(pa._single_hits(det, cfg)), ["p1", "p2"])


class SingleState(PricingAlertTestCase):
    def test_start_saves_open_episode(self):
        cfg = full_config()
        cur = make_window(errors={"akparts": make_error(770)})
        base = make_window(errors={"akparts": make_error(244)})
        det = pa._detect(cur, base, cfg)
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state = pa._load_state()

        decision, patch = pa._handle_single(cur, base, det, cfg, TF, TT, state, now_s)

        self.assertEqual(decision, "single_provider")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Отдельный поставщик", self.sent[0][1])
        self.assertIn("Не глобальная деградация", self.sent[0][1])
        pa._save_state({**state, **patch})
        saved = read_state()
        self.assertTrue(saved["single_active"])
        self.assertEqual(saved["single_providers"], ["akparts"])
        self.assertTrue(saved["single_last_notify"])

    def test_repeat_inside_interval_is_silent_and_unchanged(self):
        cfg = full_config()
        cur = make_window(errors={"akparts": make_error(770)})
        base = make_window(errors={"akparts": make_error(244)})
        det = pa._detect(cur, base, cfg)
        state = pa._load_state()
        state.update(
            {
                "single_active": True,
                "single_since": state["single_since"] or "2026-09-30 08:00:00",
                "single_last_notify": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "single_providers": ["akparts"],
            }
        )

        decision, patch = pa._handle_single(cur, base, det, cfg, TF, TT, state, "now")

        self.assertEqual(decision, "single_provider_silent")
        self.assertEqual(self.sent, [])
        self.assertEqual(patch, {})

    def test_over_limit_providers_do_not_close_open_episode(self):
        cfg = full_config()
        det = pa._detect(
            make_window(runtime={"p1": make_runtime(25.0), "p2": make_runtime(26.0)}),
            make_window(runtime={"p1": make_runtime(5.0), "p2": make_runtime(5.0)}),
            cfg,
        )
        state = pa._load_state()
        state.update(
            {
                "single_active": True,
                "single_since": "2026-09-30 08:00:00",
                "single_last_notify": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "single_providers": ["akparts"],
            }
        )

        decision, patch = pa._handle_single(
            make_window(), make_window(), det, cfg, TF, TT, state, "now"
        )

        self.assertIsNone(decision)
        self.assertEqual(self.sent, [])
        self.assertEqual(patch, {})

    def test_throttle_survives_after_recovery(self):
        cfg = full_config()
        every = float(cfg["single_provider_every_hours"])
        now = datetime.now()
        cur = make_window(errors={"akparts": make_error(770)})
        base = make_window(errors={"akparts": make_error(244)})
        det = pa._detect(cur, base, cfg)

        just_finished = {
            "active": False,
            "single_active": False,
            "single_since": "",
            "single_last_notify": now.strftime("%Y-%m-%d %H:%M:%S"),
            "single_providers": [],
        }
        decision, _ = pa._handle_single(cur, base, det, cfg, TF, TT, just_finished, "now")
        self.assertEqual(decision, "single_provider_silent")
        self.assertEqual(self.sent, [])

        past = now - timedelta(seconds=every * 3600 + 60)
        old_state = {**just_finished, "single_last_notify": past.strftime("%Y-%m-%d %H:%M:%S")}
        decision, _ = pa._handle_single(cur, base, det, cfg, TF, TT, old_state, "now")
        self.assertEqual(decision, "single_provider")
        self.assertEqual(len(self.sent), 1)

        just_under = now - timedelta(seconds=every * 3600 - 60)
        young_state = {
            **just_finished,
            "single_last_notify": just_under.strftime("%Y-%m-%d %H:%M:%S"),
        }
        decision, _ = pa._handle_single(cur, base, det, cfg, TF, TT, young_state, "now")
        self.assertEqual(decision, "single_provider_silent")

    def test_recovery_is_single_and_keeps_throttle_clock(self):
        cfg = full_config()
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state = pa._load_state()
        state.update(
            {
                "single_active": True,
                "single_since": "2026-09-30 08:00:00",
                "single_last_notify": now_s,
                "single_providers": ["akparts"],
            }
        )
        clean = pa._detect(make_window(), make_window(), cfg)

        decision, patch = pa._handle_single(
            make_window(), make_window(), clean, cfg, TF, TT, state, now_s
        )

        self.assertEqual(decision, "single_provider_recovery")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("akparts", self.sent[0][1])
        self.assertFalse(patch["single_active"])
        self.assertEqual(patch["single_providers"], [])
        self.assertNotIn("single_last_notify", patch)
        pa._save_state({**state, **patch})
        self.assertEqual(read_state()["single_last_notify"], now_s)

    def test_closed_episode_stays_quiet(self):
        cfg = full_config()
        clean = pa._detect(make_window(), make_window(), cfg)
        decision, _ = pa._handle_single(
            make_window(), make_window(), clean, cfg, TF, TT, pa._load_state(), "now"
        )

        self.assertIsNone(decision)
        self.assertEqual(self.sent, [])

    def test_save_state_does_not_merge_previous_disk_values(self):
        active = pa._load_state()
        active.update(
            {
                "single_active": True,
                "single_since": "2026-09-30 08:00:00",
                "single_last_notify": "2026-09-30 08:00:00",
                "single_providers": ["akparts"],
            }
        )
        pa._save_state(active)

        reset = {
            "active": False,
            "active_since": "",
            "last_notify": "",
            "alerted_hours": 0,
        }
        before = dict(reset)
        pa._save_state(reset)
        saved = read_state()

        self.assertEqual(reset, before)
        self.assertFalse(saved["single_active"])
        self.assertEqual(saved["single_providers"], [])
        self.assertEqual(saved["single_last_notify"], "")


class SingleMessage(PricingAlertTestCase):
    def test_two_provider_message_and_plural_form(self):
        cfg = full_config(single_provider_max_n=2)
        det = pa._detect(
            make_window(runtime={"p1": make_runtime(25.0), "p2": make_runtime(26.0)}),
            make_window(runtime={"p1": make_runtime(5.0), "p2": make_runtime(5.0)}),
            full_config(),
        )
        body = pa._build_single_message(det, cfg, TF, TT, ["p1", "p2"], 180)

        self.assertIn("p1", body)
        self.assertIn("p2", body)
        self.assertIn("2 из 180 поставщиков на грани", body)
        self.assertIn("Поставщики деградировали", body)
        self.assertIn("viewPanel=25", body)
        self.assertIn("viewPanel=27", body)

    def test_recovery_message_has_both_dashboard_links(self):
        body = pa._build_single_recovery_message(TF, TT, ["akparts"])

        self.assertIn("akparts", body)
        self.assertIn("viewPanel=25", body)
        self.assertIn("viewPanel=27", body)


class SingleEventLog(PricingAlertTestCase):
    def test_single_decision_records_single_thresholds(self):
        cfg = full_config()
        cur = make_window(errors={"akparts": make_error(770)})
        base = make_window(errors={"akparts": make_error(244)})
        det = pa._detect(cur, base, cfg)

        pa._log_event("single_provider", cur, base, det, cfg, TF, TT)
        record = json.loads(pa.EVENTS_LOG.read_text(encoding="utf-8").splitlines()[-1])

        self.assertEqual(record["decision"], "single_provider")
        self.assertEqual(record["thresholds"]["single_every_hours"],
                         cfg["single_provider_every_hours"])
        self.assertEqual(record["thresholds"]["single_error_pct"],
                         cfg["single_provider_error_pct"])
        self.assertEqual(record["thresholds"]["single_runtime_sec"],
                         cfg["single_provider_runtime_sec"])
        self.assertEqual(record["thresholds"]["single_max_n"],
                         cfg["single_provider_max_n"])


class SingleMain(PricingAlertTestCase):
    def run_main(self, cur, base, cfg):
        with (
            mock.patch.object(pa, "_window_cur", return_value=cur),
            mock.patch.object(pa, "_window", return_value=cur),
            mock.patch.object(pa, "_base_for", return_value=base),
            mock.patch.object(pa, "_watchdog_counts",
                              return_value={"n": 1000, "avg_t": 1.0}),
            mock.patch.object(pa, "_refresh_registry_if_due",
                              side_effect=lambda reg, cfg, now_s: reg),
            mock.patch.object(pa, "_load_config", return_value=cfg),
            mock.patch.object(pa, "_runs_phase2", return_value=([], 0.0)),
            mock.patch.object(pa, "_attach_trends", return_value=None),
        ):
            # Каждый прогон — как новый час: сбрасываем метку дедупа,
            # чтобы идти полным разбором (гибрид тестируется отдельно).
            try:
                st = json.loads(pa.STATE.read_text(encoding="utf-8"))
                st["last_full_hour"] = ""
                pa.STATE.write_text(json.dumps(st), encoding="utf-8")
            except (OSError, json.JSONDecodeError):
                pass
            return pa.main()

    def test_single_failure_recovery_and_throttle(self):
        cfg = full_config()
        every = float(cfg["single_provider_every_hours"])
        cur = make_window(errors={"akparts": make_error(770)})
        base = make_window(errors={"akparts": make_error(244)})
        clean = make_window()

        self.assertEqual(self.run_main(cur, base, cfg), 0)
        self.assertEqual(read_decisions()[-1], "single_provider")
        self.assertEqual(len(self.sent), 1)
        first_state = read_state()
        self.assertTrue(first_state["single_active"])

        self.assertEqual(self.run_main(cur, base, cfg), 0)
        self.assertEqual(read_decisions()[-1], "single_provider_silent")
        self.assertEqual(len(self.sent), 1)

        self.assertEqual(self.run_main(clean, clean, cfg), 0)
        self.assertEqual(read_decisions()[-1], "single_provider_recovery")
        self.assertEqual(len(self.sent), 2)
        recovered = read_state()
        self.assertFalse(recovered["single_active"])
        self.assertEqual(recovered["single_last_notify"],
                         first_state["single_last_notify"])

        self.assertEqual(self.run_main(cur, base, cfg), 0)
        self.assertEqual(read_decisions()[-1], "single_provider_silent")
        self.assertEqual(len(self.sent), 2)

        old = recovered.copy()
        old["single_active"] = True
        old["single_providers"] = ["akparts"]
        old["single_last_notify"] = (
            datetime.now() - timedelta(seconds=every * 3600 + 300)
        ).strftime("%Y-%m-%d %H:%M:%S")
        pa.STATE.write_text(json.dumps(old), encoding="utf-8")
        self.assertEqual(self.run_main(cur, base, cfg), 0)
        self.assertEqual(len(self.sent), 3)
        self.assertIn(f"уже {int(every)} ч", self.sent[-1][1])

    def test_over_limit_failure_does_not_close_open_episode(self):
        cfg = full_config()
        det = pa._detect(
            make_window(runtime={"p1": make_runtime(25.0), "p2": make_runtime(26.0)}),
            make_window(runtime={"p1": make_runtime(5.0), "p2": make_runtime(5.0)}),
            cfg,
        )
        self.assertEqual(sorted(pa._single_hits(det, cfg)), ["p1", "p2"])
        state = pa._load_state()
        state.update(
            {
                "single_active": True,
                "single_since": "2026-09-30 08:00:00",
                "single_last_notify": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "single_providers": ["akparts"],
            }
        )
        pa._save_state(state)
        cur = make_window(runtime={"p1": make_runtime(25.0), "p2": make_runtime(26.0)})
        base = make_window(runtime={"p1": make_runtime(5.0), "p2": make_runtime(5.0)})

        self.assertEqual(self.run_main(cur, base, cfg), 0)
        self.assertEqual(read_decisions()[-1], "normal")
        self.assertEqual(self.sent, [])
        self.assertTrue(read_state()["single_active"])

    def test_global_incident_keeps_single_throttle_explicitly(self):
        cfg = full_config()
        last_notify = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state = pa._load_state()
        state.update({"single_last_notify": last_notify})
        pa._save_state(state)
        cur = make_window(errors={name: make_error(500) for name in ("a", "b", "c")})
        base = make_window(errors={name: make_error(20) for name in ("a", "b", "c")})

        self.assertEqual(self.run_main(cur, base, cfg), 0)
        self.assertEqual(read_decisions()[-1], "incident")
        self.assertEqual(len(self.sent), 1)
        saved = read_state()
        self.assertTrue(saved["active"])
        self.assertFalse(saved["single_active"])
        self.assertEqual(saved["single_last_notify"], last_notify)

        clean = make_window()
        self.assertEqual(self.run_main(clean, clean, cfg), 0)
        self.assertEqual(read_decisions()[-1], "recovery")
        self.assertEqual(len(self.sent), 2)

    def test_old_state_format_is_upgraded(self):
        cfg = full_config()
        pa.STATE.write_text(
            json.dumps({"active": False, "since": "", "last_notify": ""}),
            encoding="utf-8",
        )
        cur = make_window(errors={"akparts": make_error(770)})
        base = make_window(errors={"akparts": make_error(244)})

        self.assertEqual(self.run_main(cur, base, cfg), 0)
        self.assertEqual(len(self.sent), 1)
        saved = read_state()
        self.assertTrue(saved["single_active"])
        self.assertIn("single_last_notify", saved)

    def test_disabled_single_tier(self):
        cfg = full_config(single_provider_notify=False)
        cur = make_window(errors={"akparts": make_error(770)})
        base = make_window(errors={"akparts": make_error(244)})

        self.assertEqual(self.run_main(cur, base, cfg), 0)
        self.assertEqual(read_decisions()[-1], "normal")
        self.assertEqual(self.sent, [])


class HybridSchedule(PricingAlertTestCase):
    def test_second_run_same_hour_is_watchdog(self):
        cfg = full_config()
        clean = make_window()
        rc, calls = self.run_main_hourly(clean, clean, cfg)
        self.assertEqual(rc, 0)
        self.assertEqual(read_decisions()[-1], "normal")
        self.assertEqual(calls, 1)

        rc, calls = self.run_main_hourly(clean, clean, cfg)
        self.assertEqual(rc, 0)
        self.assertEqual(read_decisions()[-1], "watchdog_ok")
        self.assertEqual(calls, 0)  # тяжёлых запросов нет
        self.assertEqual(self.sent, [])

    def test_watchdog_collapse_triggers_early_analysis(self):
        cfg = full_config()
        # Снапшот вчерашнего часа с объёмом 4000 → ожидаемые 1000 за 15 мин.
        h = pa._snapshot_label(__import__("time").time() - 86400)
        (pa.STATE.parent / "hourly").mkdir(parents=True, exist_ok=True)
        (pa.STATE.parent / "hourly" / f"{h}.json").write_text(
            json.dumps({"runtime": {}, "errors": {},
                        "volume": {"n": 4000}}), encoding="utf-8")
        cur = make_window(errors={"akparts": make_error(770)})
        base = make_window(errors={"akparts": make_error(244)})
        # Первый прогон — полный часовой (метка пуста), даст single_provider.
        rc, _ = self.run_main_hourly(cur, base, cfg)
        self.assertEqual(read_decisions()[-1], "single_provider")
        # Второй прогон в том же часе: watchdog видит 10 << 0.3*1000 → разбор.
        rc, calls = self.run_main_hourly(cur, base, cfg, watchdog_n=10)
        self.assertEqual(calls, 1)
        self.assertIn(read_decisions()[-1],
                      ("single_provider", "single_provider_silent"))

    def test_watchdog_fires_only_on_real_collapse(self):
        cfg = full_config()
        self.assertFalse(pa._watchdog_fires({"n": 10}, 200, cfg))  # ночь: порог
        self.assertFalse(pa._watchdog_fires({"n": 900}, 4000, cfg))  # норма
        self.assertTrue(pa._watchdog_fires({"n": 10}, 4000, cfg))  # обвал
        self.assertFalse(pa._watchdog_fires({}, 4000, cfg))

    def test_hour_label_and_volume_and_core(self):
        ts = __import__("time").time()
        label = pa._hour_label(int(ts))
        self.assertRegex(label, r"^\d{4}-\d{2}-\d{2} \d{2}:00$")
        vol = pa._volume_from_matrix({"a": {"n": 100}, "b": {"n": 50}})
        self.assertEqual(vol["n"], 150)
        reg = {"hours_total": 10, "providers": {
            "core1": {"hours_seen": 10}, "core2": {"hours_seen": 9},
            "tail": {"hours_seen": 1}}}
        self.assertEqual(pa._registry_core(reg), ["core1", "core2"])
        self.assertEqual(pa._registry_core({"hours_total": 0,
                                            "providers": {}}), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
