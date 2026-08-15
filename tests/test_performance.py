from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mbv.performance import (
    PerformanceMonitor,
    current_process_memory_bytes,
    format_performance_summary,
)


NANOSECOND = 1_000_000_000
MEGABYTE = 1024 * 1024


class MutableClock:
    def __init__(self, value: int | float = 0) -> None:
        self.value = value

    def __call__(self) -> int | float:
        return self.value


class PerformanceMonitorTests(unittest.TestCase):
    def make_monitor(
        self,
        target_fps: float = 20.0,
        *,
        window_size: int = 120,
        wall_clock: MutableClock | None = None,
        cpu_clock: MutableClock | None = None,
        memory_mb: float = 64.0,
    ) -> PerformanceMonitor:
        wall_clock = wall_clock or MutableClock(0)
        cpu_clock = cpu_clock or MutableClock(0.0)
        return PerformanceMonitor(
            target_fps,
            window_size=window_size,
            clock_ns=wall_clock,
            cpu_clock=cpu_clock,
            memory_reader=lambda: int(memory_mb * MEGABYTE),
            logical_cpu_count=1,
        )

    def test_deterministic_rolling_metrics_and_sparse_stages(self) -> None:
        monitor = self.make_monitor(window_size=3)
        monitor.record_frame(
            started_ns=0,
            finished_ns=10_000_000,
            stages_ns={"capture": 2_000_000},
        )
        monitor.record_frame(
            started_ns=100_000_000,
            finished_ns=140_000_000,
            stages_ns={"capture": 5_000_000, "player": 10_000_000},
        )
        monitor.record_frame(
            started_ns=200_000_000,
            finished_ns=250_000_000,
            stages_ns={"capture": 7_000_000},
        )
        monitor.record_frame(
            started_ns=300_000_000,
            finished_ns=360_000_000,
            stages_ns={"capture": 9_000_000, "player": 30_000_000},
        )

        snapshot = monitor.snapshot()
        self.assertEqual(snapshot.state, "running")
        self.assertEqual(snapshot.sample_count, 3)
        self.assertEqual(snapshot.total_frames, 4)
        self.assertAlmostEqual(snapshot.fps, 10.0)
        self.assertAlmostEqual(snapshot.frame_ms, 50.0)
        # P95 uses the deterministic nearest-rank rule, so three samples select the maximum.
        self.assertAlmostEqual(snapshot.frame_p95_ms, 60.0)
        self.assertAlmostEqual(snapshot.frame_max_ms, 60.0)
        # At 20 FPS the budget is 50 ms; equality is on budget, only 60 ms is over budget.
        self.assertAlmostEqual(snapshot.over_budget_ratio, 1.0 / 3.0)
        self.assertEqual(snapshot.mode, "full")

        stages = {stage.name: stage for stage in snapshot.stages}
        self.assertEqual(stages["capture"].sample_count, 3)
        self.assertAlmostEqual(stages["capture"].average_ms, 7.0)
        self.assertAlmostEqual(stages["capture"].max_ms, 9.0)
        # Missing stages are excluded from their denominator instead of being recorded as zero.
        self.assertEqual(stages["player"].sample_count, 2)
        self.assertAlmostEqual(stages["player"].average_ms, 20.0)
        self.assertAlmostEqual(stages["player"].max_ms, 30.0)

    def test_per_frame_target_override_updates_budget_and_snapshot(self) -> None:
        monitor = self.make_monitor(target_fps=10.0)
        monitor.record_frame(
            started_ns=0,
            finished_ns=60_000_000,
            stages_ns={},
            target_fps=20.0,
        )

        snapshot = monitor.snapshot()
        self.assertEqual(snapshot.target_fps, 20.0)
        self.assertEqual(snapshot.over_budget_ratio, 1.0)

    def test_mode_switch_starts_a_fresh_cadence_and_stage_window(self) -> None:
        monitor = self.make_monitor(target_fps=10.0)
        monitor.record_frame(
            started_ns=0,
            finished_ns=40_000_000,
            stages_ns={"monster": 20_000_000, "player": 10_000_000},
        )
        monitor.record_frame(
            started_ns=100_000_000,
            finished_ns=140_000_000,
            stages_ns={"monster": 20_000_000, "player": 10_000_000},
        )
        monitor.record_frame(
            started_ns=200_000_000,
            finished_ns=210_000_000,
            stages_ns={"capture": 5_000_000},
            mode="potion_only",
        )

        snapshot = monitor.snapshot()
        self.assertEqual(snapshot.mode, "potion_only")
        self.assertEqual(snapshot.sample_count, 1)
        self.assertEqual(snapshot.total_frames, 3)
        self.assertEqual(tuple(stage.name for stage in snapshot.stages), ("capture",))

    def test_formatter_marks_stale_running_loop_instead_of_showing_old_fps(self) -> None:
        wall = MutableClock(0)
        monitor = self.make_monitor(target_fps=10.0, wall_clock=wall)
        monitor.record_frame(started_ns=0, finished_ns=10_000_000, stages_ns={})
        monitor.record_frame(started_ns=100_000_000, finished_ns=110_000_000, stages_ns={})
        wall.value = 3 * NANOSECOND

        summary = format_performance_summary(monitor.snapshot())
        self.assertIn("主循环停滞", summary)
        self.assertNotIn("FPS 10.0", summary)

    def test_invalid_frame_values_do_not_mutate_statistics(self) -> None:
        monitor = self.make_monitor()
        invalid_calls = (
            {"started_ns": -1, "finished_ns": 1, "stages_ns": {}},
            {"started_ns": 2, "finished_ns": 1, "stages_ns": {}},
            {"started_ns": 0, "finished_ns": 1, "stages_ns": {"capture": -1}},
            {"started_ns": 0, "finished_ns": 1, "stages_ns": {"unknown": 1}},
            {"started_ns": 0, "finished_ns": 1, "stages_ns": {}, "target_fps": 0},
            {"started_ns": 0, "finished_ns": 1, "stages_ns": {}, "mode": ""},
        )

        for kwargs in invalid_calls:
            with self.subTest(kwargs=kwargs), self.assertRaises((TypeError, ValueError)):
                monitor.record_frame(**kwargs)

        snapshot = monitor.snapshot()
        self.assertEqual(snapshot.sample_count, 0)
        self.assertEqual(snapshot.total_frames, 0)

    def test_constructor_rejects_invalid_target_and_window(self) -> None:
        dependencies = {
            "clock_ns": lambda: 0,
            "cpu_clock": lambda: 0.0,
            "memory_reader": lambda: 0,
        }
        for target_fps in (0, -1):
            with self.subTest(target_fps=target_fps), self.assertRaises(ValueError):
                PerformanceMonitor(target_fps, **dependencies)
        with self.assertRaises(ValueError):
            PerformanceMonitor(20, window_size=0, **dependencies)
        for logical_cpu_count in (0, -1):
            with self.subTest(logical_cpu_count=logical_cpu_count), self.assertRaises(ValueError):
                PerformanceMonitor(20, logical_cpu_count=logical_cpu_count, **dependencies)

    def test_windows_process_memory_reader_reports_working_set(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows-only process memory reader")
        memory_bytes = current_process_memory_bytes()
        self.assertIsNotNone(memory_bytes)
        self.assertGreater(memory_bytes, 0)

    def test_suspend_ignores_frames_and_resume_resets_cadence_window(self) -> None:
        monitor = self.make_monitor(target_fps=10.0)
        monitor.record_frame(started_ns=0, finished_ns=10_000_000, stages_ns={})
        monitor.record_frame(started_ns=100_000_000, finished_ns=110_000_000, stages_ns={})
        monitor.set_suspended(True, now_ns=200_000_000)
        monitor.record_frame(started_ns=300_000_000, finished_ns=310_000_000, stages_ns={})

        suspended = monitor.snapshot()
        self.assertEqual(suspended.state, "suspended")
        self.assertEqual(suspended.total_frames, 2)
        self.assertEqual(suspended.sample_count, 2)

        monitor.set_suspended(False, now_ns=10 * NANOSECOND)
        resumed = monitor.snapshot()
        self.assertEqual(resumed.state, "warming_up")
        self.assertEqual(resumed.total_frames, 2)
        self.assertEqual(resumed.sample_count, 0)
        self.assertEqual(resumed.fps, 0.0)

        monitor.record_frame(
            started_ns=10 * NANOSECOND,
            finished_ns=10 * NANOSECOND + 20_000_000,
            stages_ns={},
        )
        monitor.record_frame(
            started_ns=10 * NANOSECOND + 100_000_000,
            finished_ns=10 * NANOSECOND + 120_000_000,
            stages_ns={},
        )
        self.assertAlmostEqual(monitor.snapshot().fps, 10.0)

    def test_capture_failures_are_recoverable_and_not_counted_as_frames(self) -> None:
        monitor = self.make_monitor()
        monitor.record_capture_failure(OSError("capture lost"), now_ns=10)
        monitor.record_capture_failure(OSError("capture still lost"), now_ns=20)

        failed = monitor.snapshot()
        self.assertEqual(failed.sample_count, 0)
        self.assertEqual(failed.total_frames, 0)
        self.assertEqual(failed.consecutive_failures, 2)
        self.assertNotEqual(failed.state, "error")
        self.assertIn("capture still lost", failed.error)

        monitor.record_frame(started_ns=30, finished_ns=40, stages_ns={})
        recovered = monitor.snapshot()
        self.assertEqual(recovered.total_frames, 1)
        self.assertEqual(recovered.consecutive_failures, 0)
        self.assertFalse(recovered.error)

    def test_fatal_error_is_sticky_and_preserves_last_good_metrics(self) -> None:
        monitor = self.make_monitor()
        monitor.record_frame(started_ns=0, finished_ns=10_000_000, stages_ns={})
        monitor.mark_error(RuntimeError("worker boom"), now_ns=20_000_000)
        monitor.record_frame(started_ns=30_000_000, finished_ns=40_000_000, stages_ns={})
        monitor.stop(now_ns=50_000_000)

        snapshot = monitor.snapshot()
        self.assertEqual(snapshot.state, "error")
        self.assertEqual(snapshot.total_frames, 1)
        self.assertEqual(snapshot.sample_count, 1)
        self.assertIn("RuntimeError", snapshot.error)
        self.assertIn("worker boom", snapshot.error)

    def test_normal_stop_is_sticky_and_ignores_later_frames(self) -> None:
        monitor = self.make_monitor()
        monitor.record_frame(started_ns=0, finished_ns=10, stages_ns={})
        monitor.stop(now_ns=20)
        monitor.record_frame(started_ns=30, finished_ns=40, stages_ns={})

        snapshot = monitor.snapshot()
        self.assertEqual(snapshot.state, "stopped")
        self.assertEqual(snapshot.total_frames, 1)

    def test_snapshot_and_stage_values_are_frozen(self) -> None:
        monitor = self.make_monitor()
        monitor.record_frame(
            started_ns=0,
            finished_ns=10_000_000,
            stages_ns={"capture": 5_000_000},
        )
        snapshot = monitor.snapshot()

        self.assertIsInstance(snapshot.stages, tuple)
        with self.assertRaises(FrozenInstanceError):
            snapshot.state = "changed"
        with self.assertRaises(FrozenInstanceError):
            snapshot.stages[0].average_ms = 999.0

    def test_cpu_and_memory_are_sampled_deterministically_on_snapshot(self) -> None:
        wall = MutableClock(0)
        cpu = MutableClock(0.0)
        memory_bytes = [64 * MEGABYTE]
        monitor = PerformanceMonitor(
            20.0,
            clock_ns=wall,
            cpu_clock=cpu,
            memory_reader=lambda: memory_bytes[0],
            logical_cpu_count=4,
        )

        monitor.record_frame(started_ns=0, finished_ns=0, stages_ns={})
        initial = monitor.snapshot()
        self.assertEqual(initial.cpu_percent, 0.0)
        self.assertAlmostEqual(initial.memory_mb, 64.0)

        wall.value = 2 * NANOSECOND
        # 两秒内占满一个核心，在四个逻辑处理器上等同任务管理器的 25%。
        cpu.value = 2.0
        memory_bytes[0] = 96 * MEGABYTE
        monitor.record_frame(
            started_ns=2 * NANOSECOND,
            finished_ns=2 * NANOSECOND,
            stages_ns={},
        )
        sampled = monitor.snapshot()
        self.assertAlmostEqual(sampled.cpu_percent, 25.0)
        self.assertAlmostEqual(sampled.memory_mb, 96.0)

    def test_snapshot_is_safe_during_concurrent_recording(self) -> None:
        monitor = self.make_monitor(window_size=32)
        done = threading.Event()
        failures: list[BaseException] = []

        def writer() -> None:
            try:
                for index in range(2_000):
                    started = index * 10_000_000
                    monitor.record_frame(
                        started_ns=started,
                        finished_ns=started + 2_000_000,
                        stages_ns={"capture": 1_000_000},
                    )
            except BaseException as exc:  # pragma: no cover - reported in the main test thread
                failures.append(exc)
            finally:
                done.set()

        def reader() -> None:
            reads = 0
            try:
                while not done.is_set() or reads < 100:
                    snapshot = monitor.snapshot()
                    self.assertLessEqual(snapshot.sample_count, 32)
                    self.assertGreaterEqual(snapshot.fps, 0.0)
                    self.assertGreaterEqual(snapshot.frame_ms, 0.0)
                    self.assertGreaterEqual(snapshot.frame_p95_ms, 0.0)
                    self.assertGreaterEqual(snapshot.frame_max_ms, 0.0)
                    reads += 1
            except BaseException as exc:  # pragma: no cover - reported in the main test thread
                failures.append(exc)

        writer_thread = threading.Thread(target=writer, name="performance-writer")
        reader_thread = threading.Thread(target=reader, name="performance-reader")
        writer_thread.start()
        reader_thread.start()
        writer_thread.join(timeout=3.0)
        reader_thread.join(timeout=3.0)

        self.assertFalse(writer_thread.is_alive(), "writer deadlocked")
        self.assertFalse(reader_thread.is_alive(), "reader deadlocked")
        self.assertEqual(failures, [])
        final = monitor.snapshot()
        self.assertEqual(final.total_frames, 2_000)
        self.assertEqual(final.sample_count, 32)

    def test_formatter_uses_chinese_status_and_runtime_metrics(self) -> None:
        monitor = self.make_monitor()
        self.assertIn("等待性能数据", format_performance_summary(monitor.snapshot()))

        monitor.record_frame(
            started_ns=0,
            finished_ns=20_000_000,
            stages_ns={"capture": 5_000_000},
        )
        monitor.record_frame(
            started_ns=50_000_000,
            finished_ns=70_000_000,
            stages_ns={"capture": 5_000_000},
        )
        running = format_performance_summary(monitor.snapshot())
        for label in ("FPS", "帧", "P95", "CPU", "内存"):
            with self.subTest(label=label):
                self.assertIn(label, running)

        monitor.set_suspended(True, now_ns=80_000_000)
        self.assertIn("暂停", format_performance_summary(monitor.snapshot()))
        monitor.set_suspended(False, now_ns=90_000_000)
        monitor.mark_error(RuntimeError("boom"), now_ns=100_000_000)
        error_text = format_performance_summary(monitor.snapshot())
        self.assertIn("异常", error_text)
        self.assertIn("boom", error_text)

        stopped_monitor = self.make_monitor()
        stopped_monitor.stop(now_ns=0)
        self.assertIn("已停止", format_performance_summary(stopped_monitor.snapshot()))

    def test_old_config_receives_default_performance_monitor_settings(self) -> None:
        from mbv.config import load_config

        with (ROOT / "config.example.json").open("r", encoding="utf-8") as handle:
            config = json.load(handle)
        config.pop("performance_monitor", None)

        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            loaded = load_config(path)

        self.assertEqual(
            loaded["performance_monitor"],
            {"visible": True, "refresh_interval_seconds": 1.0},
        )

    def test_performance_monitor_config_normalizes_visibility_and_interval(self) -> None:
        from mbv.config import load_config

        with (ROOT / "config.example.json").open("r", encoding="utf-8") as handle:
            base_config = json.load(handle)
        cases = (
            ("yes", "invalid", True, 1.0),
            (None, -3, True, 0.5),
            (1, 99, True, 5.0),
            (False, "2.5", False, 2.5),
        )

        with TemporaryDirectory() as temp_dir:
            for index, (visible, interval, expected_visible, expected_interval) in enumerate(cases):
                with self.subTest(visible=visible, interval=interval):
                    config = json.loads(json.dumps(base_config))
                    config["performance_monitor"] = {
                        "visible": visible,
                        "refresh_interval_seconds": interval,
                    }
                    path = Path(temp_dir) / f"config-{index}.json"
                    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
                    loaded = load_config(path)

                    self.assertIs(
                        loaded["performance_monitor"]["visible"],
                        expected_visible,
                    )
                    self.assertEqual(
                        loaded["performance_monitor"]["refresh_interval_seconds"],
                        expected_interval,
                    )

    def test_panel_performance_refresh_snapshots_and_reschedules_once(self) -> None:
        from mbv.panel import ControlPanel

        panel = ControlPanel.__new__(ControlPanel)
        panel.root = MagicMock()
        panel.root.after.return_value = "performance-after-1"
        panel.performance_visible = MagicMock()
        panel.performance_visible.get.return_value = True
        panel.performance_text = MagicMock()
        panel._performance_after_id = None
        panel._performance_interval_ms = 1_750

        snapshot = self.make_monitor().snapshot()
        snapshot_method = MagicMock(return_value=snapshot)
        performance = SimpleNamespace(snapshot=snapshot_method)
        # panel uses object.__getattribute__ to avoid MagicMock's auto-created attributes,
        # so the bot wrapper deliberately exposes a real performance attribute.
        panel.bot = SimpleNamespace(performance=performance)

        panel._refresh_performance_monitor()

        snapshot_method.assert_called_once_with()
        panel.performance_text.set.assert_called_once_with(
            format_performance_summary(snapshot)
        )
        panel.root.after.assert_called_once_with(
            1_750,
            panel._refresh_performance_monitor,
        )
        self.assertEqual(panel._performance_after_id, "performance-after-1")

    def test_reloaded_performance_visibility_reschedules_or_cancels_refresh(self) -> None:
        from mbv.panel import ControlPanel

        panel = ControlPanel.__new__(ControlPanel)
        panel.performance_visible = MagicMock()
        panel._render_performance_monitor_visibility = MagicMock()
        panel._schedule_performance_refresh = MagicMock()
        panel._cancel_performance_refresh = MagicMock()

        panel._load_performance_monitor_settings(
            {"performance_monitor": {"visible": True, "refresh_interval_seconds": 1.75}}
        )
        panel.performance_visible.set.assert_called_once_with(True)
        panel._render_performance_monitor_visibility.assert_called_once_with()
        panel._schedule_performance_refresh.assert_called_once_with(delay_ms=0)
        panel._cancel_performance_refresh.assert_not_called()
        self.assertEqual(panel._performance_interval_ms, 1_750)

        panel.performance_visible.reset_mock()
        panel._render_performance_monitor_visibility.reset_mock()
        panel._schedule_performance_refresh.reset_mock()
        panel._cancel_performance_refresh.reset_mock()
        panel._load_performance_monitor_settings(
            {"performance_monitor": {"visible": False, "refresh_interval_seconds": 0.5}}
        )
        panel.performance_visible.set.assert_called_once_with(False)
        panel._schedule_performance_refresh.assert_not_called()
        panel._cancel_performance_refresh.assert_called_once_with()
        self.assertEqual(panel._performance_interval_ms, 500)


if __name__ == "__main__":
    unittest.main()
