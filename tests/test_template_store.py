from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mbv.template_store import (  # noqa: E402
    TemplateRoots,
    UNCATEGORIZED,
    create_monster_category,
    list_monster_categories,
    list_monster_template_items,
    list_template_items,
    rename_monster_category,
    trash_monster_category,
    trash_monster_template,
    trash_template,
    validate_category_name,
)
from mbv import template_store  # noqa: E402


class TemplateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.monsters = self.base / "monsters"
        self.filters = self.base / "filters"
        self.player = self.base / "player"
        self.head = self.base / "player_head"
        self.title = self.base / "player_title"
        self.trash = self.base / "trash"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _roots(self) -> dict[str, Path]:
        return {"monster_root": self.monsters, "filter_root": self.filters}

    def _template_roots(self) -> TemplateRoots:
        return TemplateRoots(
            monster=self.monsters,
            filter=self.filters,
            player=self.player,
            head=self.head,
            title=self.title,
        )

    def test_all_template_kinds_list_only_direct_pngs_in_name_order(self):
        roots = self._template_roots()
        directories = {
            "monster": self.monsters,
            "filter": self.filters,
            "player": self.player,
            "head": self.head,
            "title": self.title,
        }
        for kind, directory in directories.items():
            directory.mkdir(parents=True)
            (directory / "z-last.png").write_bytes(kind.encode())
            (directory / "Alpha.png").write_bytes(kind.encode())
            (directory / "notes.txt").write_text("not a template", encoding="utf-8")
            nested = directory / "nested"
            nested.mkdir()
            (nested / "hidden.png").write_bytes(b"nested")

            with self.subTest(kind=kind):
                items = list_template_items(kind, roots=roots)
                self.assertEqual([item.filename for item in items], ["Alpha.png", "z-last.png"])
                self.assertTrue(all(item.kind == kind for item in items))
                self.assertTrue(all(item.category == UNCATEGORIZED for item in items))
                self.assertEqual([item.path for item in items], [directory / item.filename for item in items])

    def test_player_anchor_templates_are_soft_deleted_without_touching_siblings(self):
        roots = self._template_roots()
        directories = {
            "player": self.player,
            "head": self.head,
            "title": self.title,
        }
        for kind, directory in directories.items():
            directory.mkdir(parents=True)
            source = directory / f"{kind}.png"
            sibling = directory / f"{kind}-keep.png"
            source.write_bytes(f"deleted-{kind}".encode())
            sibling.write_bytes(f"kept-{kind}".encode())

            with self.subTest(kind=kind):
                recovered = trash_template(
                    kind,
                    source.name,
                    roots=roots,
                    trash_root=self.trash,
                )
                self.assertFalse(source.exists())
                self.assertEqual(sibling.read_bytes(), f"kept-{kind}".encode())
                self.assertEqual(recovered.read_bytes(), f"deleted-{kind}".encode())
                recovered.resolve().relative_to(self.trash.resolve())

    def test_generic_template_api_rejects_unsafe_kind_category_and_filename(self):
        roots = self._template_roots()
        self.player.mkdir(parents=True)
        (self.player / "player.png").write_bytes(b"player")
        outside = self.base / "outside.png"
        outside.write_bytes(b"outside")

        with self.assertRaises(ValueError):
            list_template_items("unknown", roots=roots)
        with self.assertRaises(ValueError):
            trash_template("unknown", "player.png", roots=roots, trash_root=self.trash)
        with self.assertRaises(ValueError):
            list_template_items("player", category="怪物分类", roots=roots)
        with self.assertRaises(ValueError):
            trash_template(
                "player",
                "player.png",
                category="怪物分类",
                roots=roots,
                trash_root=self.trash,
            )
        for filename in ("../outside.png", "..\\outside.png", "player.jpg"):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                trash_template(
                    "player",
                    filename,
                    roots=roots,
                    trash_root=self.trash,
                )

        self.assertEqual((self.player / "player.png").read_bytes(), b"player")
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_generic_template_delete_cleans_trash_group_when_move_fails(self):
        roots = self._template_roots()
        self.player.mkdir(parents=True)
        source = self.player / "player.png"
        source.write_bytes(b"player")

        with patch("mbv.template_store.shutil.move", side_effect=OSError("simulated move failure")):
            with self.assertRaisesRegex(OSError, "simulated"):
                trash_template(
                    "player",
                    source.name,
                    roots=roots,
                    trash_root=self.trash,
                )

        self.assertEqual(source.read_bytes(), b"player")
        trash_entries = list(self.trash.iterdir()) if self.trash.exists() else []
        self.assertEqual(trash_entries, [])

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
