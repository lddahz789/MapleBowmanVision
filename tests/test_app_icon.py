from __future__ import annotations

import ast
import ctypes
import gc
import hashlib
import inspect
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import tkinter as tk
import unittest
from unittest.mock import MagicMock, call, patch

from PIL import Image

from mbv import app_icon


ROOT = Path(__file__).resolve().parents[1]


class AppIdentityTests(unittest.TestCase):
    def test_windows_identity_is_stable_and_has_explicit_ctypes_signature(self) -> None:
        shell = MagicMock()
        api = shell.SetCurrentProcessExplicitAppUserModelID
        api.return_value = 0
        with patch.object(app_icon.sys, "platform", "win32"), patch.object(
            app_icon.ctypes, "windll", shell32_loader(shell), create=True
        ):
            self.assertTrue(app_icon.configure_app_identity())
        api.assert_called_once_with("MapleBowmanVision.ControlPanel")
        self.assertEqual(api.argtypes, [ctypes.c_wchar_p])
        self.assertIs(api.restype, ctypes.c_long)

    def test_non_windows_does_not_access_win32_api(self) -> None:
        loader = MagicMock()
        with patch.object(app_icon.sys, "platform", "linux"), patch.object(
            app_icon.ctypes, "windll", loader, create=True
        ):
            self.assertFalse(app_icon.configure_app_identity())
        self.assertEqual(loader.mock_calls, [])

    def test_failed_hresult_does_not_raise(self) -> None:
        shell = MagicMock()
        shell.SetCurrentProcessExplicitAppUserModelID.return_value = -2147467259
        with patch.object(app_icon.sys, "platform", "win32"), patch.object(
            app_icon.ctypes, "windll", shell32_loader(shell), create=True
        ):
            self.assertFalse(app_icon.configure_app_identity())

    def test_unavailable_or_denied_api_does_not_raise(self) -> None:
        for error in (AttributeError("API unavailable"), OSError("access denied")):
            with self.subTest(error=type(error).__name__):
                shell = MagicMock()
                shell.SetCurrentProcessExplicitAppUserModelID.side_effect = error
                with patch.object(app_icon.sys, "platform", "win32"), patch.object(
                    app_icon.ctypes, "windll", shell32_loader(shell), create=True
                ):
                    self.assertFalse(app_icon.configure_app_identity())


def shell32_loader(shell: MagicMock) -> MagicMock:
    loader = MagicMock()
    loader.shell32 = shell
    return loader


class AppIconTests(unittest.TestCase):
    def test_asset_is_independent_public_png_with_expected_size_and_transparency(self) -> None:
        self.assertEqual(app_icon.APP_ICON_PATH, ROOT / "resources" / "app_icon.png")
        self.assertTrue(app_icon.APP_ICON_PATH.is_absolute())
        self.assertEqual(
            hashlib.sha256(app_icon.APP_ICON_PATH.read_bytes()).hexdigest(),
            "1d18081a10b161fbb0fee6c91487220f00c9162da7a12f87c711749bdf28728f",
        )
        with Image.open(app_icon.APP_ICON_PATH) as icon:
            icon.verify()
        with Image.open(app_icon.APP_ICON_PATH) as icon:
            self.assertEqual(icon.format, "PNG")
            self.assertEqual(icon.size, (63, 58))
            alpha = icon.convert("RGBA").getchannel("A")
            self.assertEqual(alpha.getextrema(), (0, 255))

    def test_install_uses_default_icon_for_current_and_future_windows(self) -> None:
        root = MagicMock()
        image = MagicMock()
        with patch.object(app_icon.tk, "PhotoImage", return_value=image) as create, patch.object(
            app_icon, "configure_app_identity"
        ) as identity:
            self.assertTrue(app_icon.install_app_icon(root))
        create.assert_called_once_with(master=root, file=str(app_icon.APP_ICON_PATH))
        self.assertEqual(root.mock_calls, [call.iconphoto(True, image)])
        self.assertIs(root._mbv_app_icon, image)
        identity.assert_not_called()

    def test_resource_location_does_not_depend_on_working_directory(self) -> None:
        original = Path.cwd()
        with TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                root = MagicMock()
                with patch.object(app_icon.tk, "PhotoImage") as create:
                    self.assertTrue(app_icon.install_app_icon(root))
                self.assertEqual(create.call_args.kwargs["file"], str(ROOT / "resources" / "app_icon.png"))
            finally:
                os.chdir(original)

    def test_missing_asset_does_not_create_photo_or_modify_root(self) -> None:
        root = MagicMock()
        with TemporaryDirectory() as directory, patch.object(
            app_icon, "APP_ICON_PATH", Path(directory) / "missing.png"
        ), patch.object(app_icon.tk, "PhotoImage") as create:
            self.assertFalse(app_icon.install_app_icon(root))
        create.assert_not_called()
        self.assertEqual(root.mock_calls, [])
        self.assertNotIn("_mbv_app_icon", root.__dict__)

    def test_unreadable_or_corrupt_asset_does_not_modify_root(self) -> None:
        for error in (OSError("unreadable"), tk.TclError("couldn't recognize data in image file")):
            with self.subTest(error=type(error).__name__):
                root = MagicMock()
                with patch.object(app_icon.tk, "PhotoImage", side_effect=error):
                    self.assertFalse(app_icon.install_app_icon(root))
                self.assertEqual(root.mock_calls, [])
                self.assertNotIn("_mbv_app_icon", root.__dict__)

    def test_iconphoto_failure_preserves_prior_reference(self) -> None:
        root = MagicMock()
        previous = object()
        root._mbv_app_icon = previous
        root.iconphoto.side_effect = tk.TclError("window was destroyed")
        with patch.object(app_icon.tk, "PhotoImage"):
            self.assertFalse(app_icon.install_app_icon(root))
        self.assertIs(root._mbv_app_icon, previous)

    def test_module_does_not_import_config_input_or_game_window_services(self) -> None:
        tree = ast.parse(inspect.getsource(app_icon))
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports.extend(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
        self.assertFalse(any(name and name.startswith("mbv") for name in imports))


class HiddenAppIconTests(unittest.TestCase):
    """只创建立即隐藏的 Tk 根和子窗口，不连接游戏、不映射窗口。"""

    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.errors: list[BaseException] = []
        self.root.report_callback_exception = lambda _kind, error, _trace: self.errors.append(error)

    def test_png_loads_and_remains_alive_without_mapping_root_or_child(self) -> None:
        with patch.object(app_icon, "configure_app_identity") as identity:
            self.assertTrue(app_icon.install_app_icon(self.root))
            gc.collect()
            icon = self.root._mbv_app_icon
            self.assertEqual((icon.width(), icon.height()), (63, 58))
            self.assertIn(str(icon), self.root.tk.splitlist(self.root.tk.call("image", "names")))
            child = tk.Toplevel(self.root)
            child.withdraw()
            self.root.update_idletasks()
            self.assertEqual(self.root.state(), "withdrawn")
            self.assertEqual(child.state(), "withdrawn")
            child.destroy()
        identity.assert_not_called()
        self.assertEqual(self.errors, [])

    def test_reinstall_releases_old_photo_reference(self) -> None:
        self.assertTrue(app_icon.install_app_icon(self.root))
        first_name = str(self.root._mbv_app_icon)
        self.assertTrue(app_icon.install_app_icon(self.root))
        gc.collect()
        names = self.root.tk.splitlist(self.root.tk.call("image", "names"))
        self.assertNotIn(first_name, names)
        self.assertIn(str(self.root._mbv_app_icon), names)
        self.assertEqual(self.root.state(), "withdrawn")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
