from __future__ import annotations

import ctypes
import unittest
from unittest.mock import MagicMock, patch

from mbv.shared_state_probe import isolated_trial, run_attached_trial


class SharedStateTests(unittest.TestCase):
    def setUp(self):
        self.api = MagicMock()
        self.api.GetKeyState.side_effect = lambda code: 0x8000 if self.state[code] & 128 else 0
        self.state = [0] * 256
        self.state[20] = 1  # 保留 Caps Lock 的切换位。

        def get_state(buffer):
            for index, value in enumerate(self.state):
                buffer[index] = value
            return 1

        def set_state(buffer):
            self.state[:] = list(buffer)
            return 1

        def thread_id(hwnd, pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ulong)).contents.value = 42
            return 222

        self.api.GetKeyboardState.side_effect = get_state
        self.api.SetKeyboardState.side_effect = set_state
        self.api.GetWindowThreadProcessId.side_effect = thread_id
        self.patches = [patch("mbv.shared_state_probe.user32", self.api),
                        patch("mbv.shared_state_probe.configure_api"),
                        patch("mbv.shared_state_probe.window_process_path", return_value=(42, "game")),
                        patch("mbv.shared_state_probe.ctypes.windll.kernel32.GetCurrentThreadId", return_value=111),
                        patch("mbv.shared_state_probe.key_lparam", return_value=1),
                        patch("mbv.shared_state_probe.time.sleep"),
                        patch("mbv.shared_state_probe.time.monotonic", side_effect=[0, 0, .5])]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_state_only_releases_and_detaches_without_messages(self):
        result = run_attached_trial(123, 42, "left", False, MagicMock())
        self.assertTrue(result["pressed_state_readback"])
        self.assertTrue(result["released"] and result["detached"])
        self.assertEqual(self.state[37], 0)
        self.assertEqual(self.state[20], 1)
        self.assertEqual(self.api.AttachThreadInput.call_args_list[-1].args, (111, 222, False))
        self.api.PostMessageW.assert_not_called()
        self.api.SendInput.assert_not_called()
        self.api.SetForegroundWindow.assert_not_called()
        self.api.SendMessageTimeoutW.assert_not_called()

    def test_state_plus_messages_sends_down_and_up(self):
        run_attached_trial(123, 42, "right", True, MagicMock())
        self.assertEqual([call.args[:3] for call in self.api.PostMessageW.call_args_list],
                         [(123, 0x100, 39), (123, 0x101, 39)])
        self.assertEqual(self.state[39], 0)

    def test_failure_before_attach_does_not_attach_or_send(self):
        with self.assertRaises(RuntimeError):
            run_attached_trial(123, 42, "left", True, MagicMock(side_effect=RuntimeError("stop")))
        self.api.AttachThreadInput.assert_not_called()
        self.api.SetKeyboardState.assert_not_called()

    def test_failure_after_attach_still_detaches(self):
        guard = MagicMock(side_effect=[None, None, RuntimeError("stop")])
        with self.assertRaises(RuntimeError):
            run_attached_trial(123, 42, "left", True, guard)
        self.assertEqual(self.api.AttachThreadInput.call_args_list[-1].args, (111, 222, False))
        self.assertEqual(self.api.PostMessageW.call_args.args[1], 0x101)

    def test_state_read_failure_still_detaches_and_releases_message(self):
        self.api.GetKeyboardState.side_effect = None
        self.api.GetKeyboardState.return_value = 0
        with self.assertRaises(OSError):
            run_attached_trial(123, 42, "left", True, MagicMock())
        self.assertEqual(self.api.AttachThreadInput.call_args_list[-1].args, (111, 222, False))
        self.assertEqual(self.api.PostMessageW.call_args.args[1], 0x101)

    def test_failed_attach_never_sets_state(self):
        self.api.AttachThreadInput.return_value = 0
        with self.assertRaises(OSError):
            run_attached_trial(123, 42, "left", True, MagicMock())
        self.api.SetKeyboardState.assert_not_called()

    def test_failure_after_keydown_still_clears_state(self):
        self.api.PostMessageW.side_effect = [0, 1]
        with self.assertRaisesRegex(OSError, "按键消息投递失败"):
            run_attached_trial(123, 42, "left", True, MagicMock())
        self.assertEqual(self.state[37], 0)
        self.assertEqual(self.api.AttachThreadInput.call_args_list[-1].args, (111, 222, False))

    def test_failed_detach_is_reported(self):
        self.api.AttachThreadInput.side_effect = [1, 0]
        with self.assertRaisesRegex(OSError, "解除线程附加失败"):
            run_attached_trial(123, 42, "left", True, MagicMock())

    def test_state_readback_failure_is_not_success(self):
        self.api.GetKeyState.side_effect = None
        self.api.GetKeyState.return_value = 0
        with self.assertRaisesRegex(OSError, "无法读回"):
            run_attached_trial(123, 42, "left", True, MagicMock())

    def test_refuses_invalid_direction_duration_and_pid(self):
        for direction, duration in (("a", .3), ("left", 10), ("left", float("nan"))):
            with self.assertRaises(ValueError):
                run_attached_trial(123, 42, direction, True, MagicMock(), duration)
        with self.assertRaises(OSError):
            run_attached_trial(123, 43, "left", True, MagicMock())
        self.api.AttachThreadInput.assert_not_called()


class IsolatedTrialTests(unittest.TestCase):
    def setUp(self):
        self.context = MagicMock()
        self.parent = MagicMock()
        self.child = MagicMock()
        self.context.Pipe.return_value = (self.parent, self.child)
        self.process = self.context.Process.return_value
        self.process.is_alive.return_value = False
        self.parent.poll.return_value = True
        patcher = patch("mbv.shared_state_probe.multiprocessing.get_context", return_value=self.context)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_child_error_is_propagated(self):
        self.parent.recv.return_value = {"error": "detach failed"}
        with self.assertRaisesRegex(RuntimeError, "detach failed"):
            isolated_trial(123, 42, "left", False, MagicMock())
        self.parent.close.assert_called_once()
        self.process.close.assert_called_once()

    def test_guard_failure_does_not_spawn(self):
        with self.assertRaises(RuntimeError):
            isolated_trial(123, 42, "left", False, MagicMock(side_effect=RuntimeError("stop")))
        self.context.Process.assert_not_called()

    def test_cancellation_allows_finally_before_terminating(self):
        self.process.is_alive.side_effect = [True, True, False]
        with patch("mbv.shared_state_probe.window_process_path", return_value=(42, "game")), patch(
            "mbv.shared_state_probe.user32.PostMessageW"
        ) as post:
            with self.assertRaises(RuntimeError):
                isolated_trial(123, 42, "right", True, MagicMock(side_effect=[None, RuntimeError("stop")]))
            self.parent.send.assert_called_with("cancel")
            self.process.join.assert_any_call(.6)
            self.process.terminate.assert_called_once()
            self.assertEqual(post.call_args.args[:3], (123, 0x101, 39))

    def test_handshake_and_result(self):
        self.parent.recv.side_effect = [{"ready": True}, {"result": {"released": True}}]
        self.process.is_alive.side_effect = [True, False, False, False]
        self.assertEqual(isolated_trial(123, 42, "left", False, MagicMock()), {"released": True})
        self.parent.send.assert_called_with("go")
        self.process.terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
