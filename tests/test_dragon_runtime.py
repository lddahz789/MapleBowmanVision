from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, call, patch

from mbv.bot import BowmanBot
from mbv.buffs import BUFF_KEY_HOLD_SECONDS
from mbv.config import load_config
from mbv.strategies import get_strategy
from mbv.strategies.base import StrategyDecision
from mbv.window import WindowInfo, WindowTarget


ROOT = Path(__file__).resolve().parents[1]


class DragonRuntimeTests(unittest.TestCase):
    """使用实际公共执行器和药/Buff调度；所有窗口及按键调用均为替身。"""

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("Keyboard", "SessionLog", "PerformanceMonitor"):
            self.stack.enter_context(patch(f"mbv.bot.{name}"))
        self.stack.enter_context(patch("mbv.bot.load_templates", return_value=[]))
        self.user32 = self.stack.enter_context(patch("mbv.bot.user32"))
        self.user32.IsWindow.return_value = True
        self.user32.IsIconic.return_value = False
        self.user32.GetForegroundWindow.return_value = 10
        self.clock = self.stack.enter_context(patch("mbv.bot.time.monotonic", return_value=100.0))
        config = load_config(ROOT / "config.example.json")
        config["input"]["delivery"] = "foreground"
        config["keys"].update(attack="a", pickup="z", hp_potion="h", mp_potion="m")
        for buff in config["buffs"].values():
            buff["enabled"] = False
        self.bot = BowmanBot(config, input_authorized=True)
        self.window = WindowInfo(10, "测试窗口", 0, 0, 800, 600)
        self.bot.window = self.window
        self.bot.armed = True
        self.bot.started_at = 1.0
        self.bot.last_nameplate_seen_at = 100.0
        self.bot.marker_last_seen = 100.0
        self.bot.last_attack_anchor = (100.0, 100.0)
        self.bot.live_marker_unambiguous = True
        self.bot.keyboard.movement_events.return_value = []
        self.bot.keyboard.hybrid.active = False
        self.bot.keyboard.hybrid.hwnd = 10
        self.bot.keyboard.hybrid.desktop.foreground.return_value = 10
        self.decision = StrategyDecision(
            action="cast", state="DRAGON_CAST", attack_key="r",
            attack_skill="dragon_roar", attack_interval_seconds=1.0,
        )
        # 固定策略意图用于验证公共安全门，不依赖新策略的区域/数量判断。
        self.strategy = SimpleNamespace(
            key="dragon_roar", allow_player_lost_recovery=False,
            decide=MagicMock(return_value=self.decision),
        )
        self.bot.strategy = self.strategy
        self.turn = self.stack.enter_context(patch.object(self.bot, "_prepare_attack_turn"))
        self.face = self.stack.enter_context(patch.object(self.bot, "face_target"))
        self.normal_attack = self.stack.enter_context(patch.object(self.bot, "face_and_attack"))

    def act(
        self, *, now: float = 100.0, hp: float = 1.0, mp: float = 1.0,
        marker: tuple[float, float] | None = (.5, .5),
        player: tuple[int, int, int, int] | None = (90, 100, 20, 1),
    ) -> None:
        self.clock.return_value = now
        if player is not None:
            self.bot.last_nameplate_seen_at = now
        self.bot.act(self.window, hp, mp, marker, player, (130, 90, 20, 20), None,
                     400, True, now, 200)

    def cast(self, interval: object = 1.0, now: float = 100.0, key: object = "r") -> None:
        self.clock.return_value = now
        self.bot.cast_skill(key, "dragon_roar", interval, now, "DRAGON_CAST")

    def assert_no_direction_or_fallback(self) -> None:
        self.turn.assert_not_called()
        self.face.assert_not_called()
        self.normal_attack.assert_not_called()
        self.bot.keyboard.down.assert_not_called()
        self.bot.keyboard.movement_down.assert_not_called()
        self.bot.keyboard.movement_tap.assert_not_called()
        self.bot.keyboard.prepare_movement.assert_not_called()

    def test_public_executor_casts_only_requested_skill_without_facing(self) -> None:
        self.bot.direction = "left"
        self.act()
        self.bot.keyboard.tap.assert_called_once_with("r")
        self.assertEqual(self.bot.last_attack, 100.0)
        self.assertEqual(self.bot.state, "DRAGON_CAST")
        self.assertEqual(self.bot.direction, "left")
        self.assert_no_direction_or_fallback()

    def test_cast_trims_skill_key_without_using_common_attack(self) -> None:
        self.cast(key=" R ")
        self.bot.keyboard.tap.assert_called_once_with("r")
        self.assert_no_direction_or_fallback()

    def test_empty_or_invalid_skill_key_never_falls_back_to_normal_attack(self) -> None:
        for key in (None, "", "  ", 123):
            with self.subTest(key=key):
                self.bot.last_attack = 12.0
                self.cast(key=key)
                self.assertEqual(self.bot.last_attack, 12.0)
        self.bot.keyboard.tap.assert_not_called()
        self.assert_no_direction_or_fallback()

    def test_cast_interval_uses_last_successful_attack_and_honors_boundary(self) -> None:
        self.cast(interval=2.0, now=100.0)
        self.cast(interval=2.0, now=101.999)
        self.assertEqual(self.bot.keyboard.tap.call_count, 1)
        self.assertEqual(self.bot.last_attack, 100.0)
        self.cast(interval=2.0, now=102.0)
        self.assertEqual(self.bot.keyboard.tap.call_args_list, [call("r"), call("r")])
        self.assertEqual(self.bot.last_attack, 102.0)

    def test_cast_respects_existing_attack_timestamp_on_strategy_switch(self) -> None:
        self.bot.last_attack = 99.6
        self.cast(now=100.0)
        self.bot.keyboard.tap.assert_not_called()
        self.assertEqual(self.bot.last_attack, 99.6)

    def test_slow_detection_cannot_shorten_actual_cast_interval(self) -> None:
        # 帧时间为100.0，但识别结束实际已到100.9，冷却不能从旧帧时间起算。
        self.clock.return_value = 100.9
        self.bot.cast_skill("r", "dragon_roar", 1.0, 100.0, "DRAGON_CAST")
        self.assertEqual(self.bot.last_attack, 100.9)
        self.clock.return_value = 101.0
        self.bot.cast_skill("r", "dragon_roar", 1.0, 101.0, "DRAGON_CAST")
        self.bot.keyboard.tap.assert_called_once_with("r")
        self.clock.return_value = 101.91
        self.bot.cast_skill("r", "dragon_roar", 1.0, 101.8, "DRAGON_CAST")
        self.assertEqual(self.bot.keyboard.tap.call_count, 2)
        self.assertEqual(self.bot.last_attack, 101.91)

    def test_slow_key_hold_counts_from_successful_tap_completion(self) -> None:
        self.clock.return_value = 100.0

        def complete_later(_key: str) -> None:
            self.clock.return_value = 100.35

        self.bot.keyboard.tap.side_effect = complete_later
        self.bot.cast_skill("r", "dragon_roar", 1.0, 100.0, "DRAGON_CAST")
        self.assertEqual(self.bot.last_attack, 100.35)
        self.bot.keyboard.tap.side_effect = None
        self.clock.return_value = 101.0
        self.bot.cast_skill("r", "dragon_roar", 1.0, 101.0, "DRAGON_CAST")
        self.bot.keyboard.tap.assert_called_once_with("r")
        self.clock.return_value = 101.36
        self.bot.cast_skill("r", "dragon_roar", 1.0, 101.36, "DRAGON_CAST")
        self.assertEqual(self.bot.keyboard.tap.call_count, 2)
        self.assertEqual(self.bot.last_attack, 101.36)

    def test_invalid_intervals_use_one_second_default(self) -> None:
        for interval in (None, float("nan"), float("inf"), float("-inf"), "invalid", True):
            with self.subTest(interval=interval):
                self.bot.keyboard.tap.reset_mock()
                self.bot.last_attack = 100.0
                self.cast(interval=interval, now=100.5)
                self.bot.keyboard.tap.assert_not_called()
                self.cast(interval=interval, now=101.0)
                self.bot.keyboard.tap.assert_called_once_with("r")

    def test_cast_interval_is_bounded_to_point_one_through_ten_seconds(self) -> None:
        for interval in (-5.0, 0.0, .01):
            with self.subTest(interval=interval):
                self.bot.keyboard.tap.reset_mock()
                self.bot.last_attack = 100.0
                self.cast(interval=interval, now=100.05)
                self.bot.keyboard.tap.assert_not_called()
                self.cast(interval=interval, now=100.11)
                self.bot.keyboard.tap.assert_called_once_with("r")
        self.bot.keyboard.tap.reset_mock()
        self.bot.last_attack = 100.0
        self.cast(interval=100.0, now=109.99)
        self.bot.keyboard.tap.assert_not_called()
        self.cast(interval=100.0, now=110.0)
        self.bot.keyboard.tap.assert_called_once_with("r")

    def test_failed_cast_does_not_consume_cooldown_or_log_success(self) -> None:
        self.bot.last_attack = 12.0
        self.bot.keyboard.tap.side_effect = OSError("发送失败")
        with self.assertRaisesRegex(OSError, "发送失败"):
            self.cast()
        self.assertEqual(self.bot.last_attack, 12.0)
        self.bot.log.write.assert_not_called()
        self.bot.keyboard.tap.side_effect = None
        self.cast(now=100.1)
        self.assertEqual(self.bot.last_attack, 100.1)

    def test_cast_releases_pickup_and_direction_keys_before_skill(self) -> None:
        self.bot._pickup_held_key = "z"
        self.bot.move_progress = object()
        self.act()
        calls = self.bot.keyboard.method_calls
        attack_index = calls.index(call.tap("r"))
        for key in ("z", self.bot.config["keys"]["left"], self.bot.config["keys"]["right"]):
            self.assertLess(calls.index(call.up(key)), attack_index)
        self.assertIsNone(self.bot._pickup_held_key)
        self.assertIsNone(self.bot.move_progress)
        self.assert_no_direction_or_fallback()

    def test_waiting_for_cast_interval_still_releases_navigation_and_pickup(self) -> None:
        self.bot._pickup_held_key = "z"
        self.bot.last_attack = 99.9
        self.cast()
        self.bot.keyboard.up.assert_any_call("z")
        self.bot.keyboard.up.assert_any_call(self.bot.config["keys"]["left"])
        self.bot.keyboard.up.assert_any_call(self.bot.config["keys"]["right"])
        self.bot.keyboard.tap.assert_not_called()

    def test_hybrid_background_cast_ends_movement_without_activating_window(self) -> None:
        self.bot.delivery = "hybrid"
        self.bot.background_input = True
        self.bot.keyboard.hybrid.active = True
        self.user32.GetForegroundWindow.return_value = 99
        self.bot.keyboard.hybrid.desktop.foreground.return_value = 99
        self.act()
        self.bot.keyboard.tap.assert_called_once_with("r")
        self.bot.keyboard.finish_movement.assert_called()
        self.user32.SetForegroundWindow.assert_not_called()
        self.assert_no_direction_or_fallback()

    def test_minimap_only_rejects_even_a_strategy_that_requests_cast(self) -> None:
        self.bot.player_track.nameplate_identity_established = True
        self.bot.player_track.minimap_navigation_seen_at = 99.0
        self.bot.last_nameplate_seen_at = 1.0
        self.act(player=None)
        self.strategy.decide.assert_called_once()
        context = self.strategy.decide.call_args.args[0]
        self.assertTrue(context.minimap_only)
        self.assertIsNone(context.player_box)
        self.assertIsNone(context.target_box)
        self.assertEqual(context.eligible_detections, ())
        self.bot.keyboard.tap.assert_not_called()
        self.assertEqual(self.bot.state, "MINIMAP_WAITING_VISUAL")

    def test_lost_player_stops_before_first_strategy_decision_or_blind_recovery(self) -> None:
        self.bot.last_nameplate_seen_at = 1.0
        self.bot.config["behavior"]["player_lost_recovery_enabled"] = True
        with patch.object(self.bot, "recover_player_nameplate") as recovery:
            self.act(player=None)
        recovery.assert_not_called()
        self.strategy.decide.assert_not_called()
        self.bot.keyboard.tap.assert_not_called()
        self.assert_no_direction_or_fallback()

    def test_dragon_strategy_disables_recovery_before_any_runtime_state_exists(self) -> None:
        self.bot.strategy = get_strategy("dragon_roar")
        self.assertFalse(self.bot.strategy.allow_player_lost_recovery)
        self.assertEqual(self.bot.strategy_runtime_state, {})
        self.bot.last_nameplate_seen_at = 1.0
        with patch.object(self.bot, "recover_player_nameplate") as recovery:
            self.act(player=None)
        recovery.assert_not_called()
        self.bot.keyboard.tap.assert_not_called()
        self.assert_no_direction_or_fallback()

    def test_first_dragon_frame_rejects_nonempty_but_stale_player_box(self) -> None:
        self.bot.strategy = get_strategy("dragon_roar")
        self.bot.last_nameplate_seen_at = 1.0
        self.bot.player_track.last_seen_at = 99.9
        self.assertEqual(self.bot.strategy_runtime_state, {})
        self.assertFalse(self.bot.player_track.has_nameplate_identity())
        with patch.object(self.bot, "recover_player_nameplate") as recovery, \
             patch.object(self.bot.strategy, "decide") as decide:
            # 辅助定位的旧 hold 框仍非空，但本帧无真实定位或可信地图补位。
            self.bot.act(self.window, 1.0, 1.0, (.5, .5), (90, 100, 20, 1),
                         (130, 90, 20, 20), None, 400, True, 100.0, 200)
        recovery.assert_not_called()
        decide.assert_not_called()
        self.bot.keyboard.tap.assert_not_called()
        self.assertEqual(self.bot.state, "PLAYER_SCREEN_LOST")

    def test_ambiguous_minimap_marker_blocks_cast(self) -> None:
        self.bot.live_marker_unambiguous = False
        self.act()
        self.strategy.decide.assert_not_called()
        self.bot.keyboard.tap.assert_not_called()
        self.assertEqual(self.bot.state, "MARKER_LOST")

    def test_hp_potion_takes_priority_over_cast_and_due_buff(self) -> None:
        self.bot.auto_potion.set_enabled(True)
        self.bot.config["buffs"]["buff_1"].update(enabled=True, key="b", interval_seconds=30.0)
        self.act(hp=.01)
        self.bot.keyboard.tap.assert_called_once_with("h")
        self.strategy.decide.assert_not_called()
        self.assertEqual(self.bot.last_attack, 0.0)
        self.assertEqual(self.bot.state, "HP_POTION")

    def test_mp_potion_takes_priority_over_cast(self) -> None:
        self.bot.auto_potion.set_enabled(True)
        self.act(mp=.01)
        self.bot.keyboard.tap.assert_called_once_with("m")
        self.strategy.decide.assert_not_called()
        self.assertEqual(self.bot.last_attack, 0.0)

    def test_buff_preparation_cast_and_guard_all_prevent_roar(self) -> None:
        self.bot.config["buffs"]["buff_1"].update(enabled=True, key="b", interval_seconds=30.0)
        self.act(now=100.0)
        self.bot.keyboard.tap.assert_not_called()
        self.act(now=100.5)
        self.bot.keyboard.tap.assert_called_once_with("b", BUFF_KEY_HOLD_SECONDS)
        self.act(now=101.0)
        self.strategy.decide.assert_not_called()
        self.assertEqual(self.bot.keyboard.tap.call_count, 1)
        self.act(now=101.71)
        self.bot.keyboard.tap.assert_called_with("r")

    def test_closed_or_minimized_window_disarms_before_cast(self) -> None:
        for closed, minimized in ((True, False), (False, True)):
            with self.subTest(closed=closed, minimized=minimized):
                self.bot.armed = True
                self.user32.IsWindow.return_value = not closed
                self.user32.IsIconic.return_value = minimized
                self.act()
                self.assertFalse(self.bot.armed)
        self.bot.keyboard.tap.assert_not_called()
        self.strategy.decide.assert_not_called()
        self.bot.keyboard.release_all.assert_called()

    def test_foreground_mode_losing_focus_disarms_before_cast(self) -> None:
        self.user32.GetForegroundWindow.return_value = 99
        self.act()
        self.bot.keyboard.tap.assert_not_called()
        self.strategy.decide.assert_not_called()
        self.assertFalse(self.bot.armed)

    def test_selected_pid_failure_blocks_cast_before_decision(self) -> None:
        self.bot._window_target = WindowTarget(10, 123, "测试窗口", "game.exe")
        with patch("mbv.bot.resolve_window_target", side_effect=RuntimeError("进程已变化")):
            with self.assertRaisesRegex(RuntimeError, "进程已变化"):
                self.act()
        self.bot.keyboard.tap.assert_not_called()
        self.strategy.decide.assert_not_called()

    def test_paused_or_unauthorized_bot_cannot_cast(self) -> None:
        for armed, authorized in ((False, True), (True, False)):
            with self.subTest(armed=armed, authorized=authorized):
                self.bot.armed, self.bot.input_authorized = armed, authorized
                self.act()
                self.cast()
        self.bot.keyboard.tap.assert_not_called()
        self.strategy.decide.assert_not_called()

    def test_late_pause_exit_or_capture_request_blocks_cast_key(self) -> None:
        for event in (self.bot.f8_requested, self.bot.f9_requested, self.bot.vision_suspended):
            with self.subTest(event=event):
                event.set()
                self.cast()
                event.clear()
        self.bot.keyboard.tap.assert_not_called()
        self.assertEqual(self.bot.last_attack, 0.0)

    def test_pause_arriving_during_strategy_decision_prevents_key_dispatch(self) -> None:
        def decide(_context: object) -> StrategyDecision:
            self.bot.f8_requested.set()
            return self.decision

        self.strategy.decide.side_effect = decide
        self.act()
        self.bot.keyboard.tap.assert_not_called()
        self.assertEqual(self.bot.last_attack, 0.0)

    def test_cast_does_not_follow_decision_facing_coordinates(self) -> None:
        self.strategy.decide.return_value = replace(
            self.decision, direction="right", player_x=.9, target_x=.1, face_each_attack=True,
        )
        self.act()
        self.bot.keyboard.tap.assert_called_once_with("r")
        self.assert_no_direction_or_fallback()


if __name__ == "__main__":
    unittest.main()
