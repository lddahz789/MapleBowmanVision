from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mbv.template_store import (  # noqa: E402
    UNCATEGORIZED,
    create_monster_category,
    list_monster_categories,
    list_monster_template_items,
    rename_monster_category,
    trash_monster_category,
    trash_monster_template,
    validate_category_name,
)
from mbv import template_store  # noqa: E402


class TemplateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        base = Path(self.temporary.name)
        self.monsters = base / "monsters"
        self.filters = base / "filters"
        self.trash = base / "trash"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _roots(self) -> dict[str, Path]:
        return {"monster_root": self.monsters, "filter_root": self.filters}

    def test_category_crud_preserves_and_recovers_templates(self):
        created = create_monster_category("绿水灵", **self._roots())
        self.assertEqual(created, "绿水灵")
        self.assertTrue((self.monsters / created / ".gitkeep").is_file())
        self.assertTrue((self.filters / created / ".gitkeep").is_file())
        (self.monsters / created / "monster.png").write_bytes(b"monster")
        (self.filters / created / "filter.png").write_bytes(b"filter")

        categories = list_monster_categories(**self._roots())
        category = next(item for item in categories if item.name == created)
        self.assertEqual((category.monster_count, category.filter_count), (1, 1))

        renamed = rename_monster_category("绿水灵", "火独眼兽", **self._roots())
        self.assertEqual(renamed, "火独眼兽")
        self.assertTrue((self.monsters / renamed / "monster.png").is_file())
        self.assertTrue((self.filters / renamed / "filter.png").is_file())

        recovered = trash_monster_category(renamed, trash_root=self.trash, **self._roots())
        self.assertFalse((self.monsters / renamed).exists())
        self.assertFalse((self.filters / renamed).exists())
        self.assertTrue((recovered / "monsters" / renamed / "monster.png").is_file())
        self.assertTrue((recovered / "filters" / renamed / "filter.png").is_file())

    def test_individual_template_delete_is_recoverable(self):
        self.monsters.mkdir(parents=True)
        source = self.monsters / "legacy.png"
        source.write_bytes(b"legacy")
        items = list_monster_template_items(UNCATEGORIZED, "monster", **self._roots())
        self.assertEqual([item.filename for item in items], ["legacy.png"])

        recovered = trash_monster_template(
            UNCATEGORIZED,
            "monster",
            "legacy.png",
            trash_root=self.trash,
            **self._roots(),
        )
        self.assertFalse(source.exists())
        self.assertEqual(recovered.read_bytes(), b"legacy")

    def test_category_names_reject_duplicates_and_unsafe_paths(self):
        create_monster_category("Blue Snail", **self._roots())
        with self.assertRaises(FileExistsError):
            create_monster_category("blue snail", **self._roots())
        for invalid in ("", ".hidden", "..", "../outside", "a/b", "bad?name", "NUL", "尾点."):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_category_name(invalid)

    def test_category_delete_rolls_back_when_second_move_fails(self):
        category = create_monster_category("漂漂猪", **self._roots())
        monster = self.monsters / category / "monster.png"
        exclusion = self.filters / category / "filter.png"
        monster.write_bytes(b"monster")
        exclusion.write_bytes(b"filter")
        real_move = template_store.shutil.move
        calls = [0]

        def flaky_move(source: str, destination: str) -> str:
            calls[0] += 1
            if calls[0] == 2:
                raise OSError("simulated filter move failure")
            return real_move(source, destination)

        with patch("mbv.template_store.shutil.move", side_effect=flaky_move):
            with self.assertRaisesRegex(OSError, "simulated"):
                trash_monster_category(
                    category,
                    trash_root=self.trash,
                    **self._roots(),
                )

        self.assertEqual(monster.read_bytes(), b"monster")
        self.assertEqual(exclusion.read_bytes(), b"filter")
        self.assertFalse(any(self.trash.iterdir()))

    def test_uncategorized_is_always_available_but_cannot_be_removed(self):
        categories = list_monster_categories(**self._roots())
        self.assertEqual(categories[0].name, UNCATEGORIZED)
        with self.assertRaises(ValueError):
            trash_monster_category(
                UNCATEGORIZED,
                trash_root=self.trash,
                **self._roots(),
            )


if __name__ == "__main__":
    unittest.main()
