from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mbv.config import save_config


class ConfigSavingTests(unittest.TestCase):
    def test_repeated_autosave_preserves_previous_distinct_version_without_writes(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            backup = path.with_name("config.json.bak")
            save_config(path, {"value": 1, "label": "配置"})
            save_config(path, {"value": 2, "label": "配置"})
            previous = backup.read_bytes()
            current = path.read_bytes()
            with patch("mbv.config._atomic_write") as write:
                for _ in range(3):
                    save_config(path, {"value": 2, "label": "配置"})
                write.assert_not_called()
            self.assertEqual(path.read_bytes(), current)
            self.assertEqual(backup.read_bytes(), previous)
            self.assertEqual(json.loads(previous)["value"], 1)
            save_config(path, {"value": 3, "label": "配置"})
            self.assertEqual(backup.read_bytes(), current)
            self.assertEqual(json.loads(path.read_bytes())["value"], 3)

    def test_unchanged_first_save_does_not_create_a_redundant_backup(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(path, {"value": 1})
            save_config(path, {"value": 1})
            self.assertFalse(path.with_name("config.json.bak").exists())

    def test_corrupt_current_file_is_repaired_without_replacing_valid_backup(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            backup = path.with_name("config.json.bak")
            save_config(path, {"value": 1})
            save_config(path, {"value": 2})
            previous = backup.read_bytes()
            path.write_bytes(b'{"value":')
            save_config(path, {"value": 2})
            self.assertEqual(json.loads(path.read_bytes()), {"value": 2})
            self.assertEqual(backup.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
