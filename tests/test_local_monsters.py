from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

from PIL import Image

from mbv.local_monsters import LocalMonster, MonsterFrame, MonsterLibrary, import_frames, read_frame
from mbv.template_store import TemplateRoots, list_template_items, trash_template


class LocalMonsterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = MonsterLibrary(self.root)
        with closing(sqlite3.connect(self.root / 'catalog.sqlite')) as con:
            con.executescript('CREATE TABLE names(dataset TEXT, category TEXT,resource_id TEXT,name TEXT);'
                              'CREATE TABLE files(source TEXT PRIMARY KEY,metadata TEXT);')
            con.executemany('INSERT INTO names VALUES(?,?,?,?)', [
                ('Data', 'Mob', '1', '蜗牛'), ('BetaData', 'Mob', '1', '测试蜗牛'),
                ('Data', 'Npc', '1', '蜗牛'), ('Data', 'Mob', '2', '缺图蜗牛')])
            con.commit()
        self.monster = LocalMonster('Data', '1', '蜗牛')
        self.source = 'Data/Mob/0000001.img'
        self.png = self.root / 'frame.png'
        image = Image.new('RGBA', (10, 10), (0, 0, 0, 0))
        image.paste((180, 50, 30, 255), (2, 3, 8, 9))
        image.save(self.png)
        self.canvas = {'type': 'canvas', 'file': 'frame.png', 'children': {}}
        self.write_tree(self.source, {'type': 'property', 'children': {
            'stand': {'type': 'property', 'children': {'0': self.canvas}},
            'move': {'type': 'property', 'children': {'0': {'type': 'uol', 'value': '../stand/0'}}},
            'die1': {'type': 'property', 'children': {'0': self.canvas}},
        }})
        self.roots = TemplateRoots(*(self.root / name for name in ('monsters','filters','players','heads','titles')))

    def write_tree(self, source, tree):
        filename = source.replace('/', '_') + '.json'
        (self.root / filename).write_text(json.dumps({'tree': tree}), encoding='utf-8')
        with closing(sqlite3.connect(self.root / 'catalog.sqlite')) as con:
            con.execute('INSERT OR REPLACE INTO files VALUES(?,?)', (source, filename))
            con.commit()
        self.library._trees.clear()

    def test_search_name_id_and_dataset_without_missing_images(self):
        self.assertEqual(self.library.search('蜗牛'), [self.monster])
        self.assertEqual(self.library.search('0000001'), [self.monster])
        self.assertEqual(self.library.search('%'), [])
        self.assertEqual(self.library.search("' OR 1=1 --"), [])
        self.assertEqual(self.library.search('蜗牛', 'BetaData'), [])
        self.assertEqual(self.library.search(''), [])

    def test_frames_resolve_uol_and_exclude_death(self):
        frames = self.library.frames(self.monster)
        self.assertEqual([frame.node for frame in frames], ['move/0', 'stand/0'])
        self.assertTrue(all(frame.path == self.png for frame in frames))

    def test_canvas_and_whole_monster_links(self):
        self.write_tree('Data/Mob/0000003.img', {'type': 'property', 'children': {
            'info': {'children': {'link': {'value': '1'}}}}})
        self.assertEqual(len(self.library.frames(LocalMonster('Data', '3', 'linked'))), 2)
        self.write_tree(self.source, {'type': 'property', 'children': {
            'stand': {'children': {'0': {'type': 'canvas', 'children': {'_inlink': {'value': 'stand/1'}}},
                                   '1': self.canvas}}}})
        self.assertEqual(self.library.image(self.source, 'stand/0'), self.png)

    def test_cycles_and_cross_dataset_are_rejected(self):
        self.write_tree(self.source, {'type': 'property', 'children': {
            'stand': {'children': {'0': {'type': 'canvas', 'children': {'_inlink': {'value': 'stand/0'}}}}}}})
        with self.assertRaisesRegex(ValueError, '循环'):
            self.library.frames(self.monster)
        with self.assertRaisesRegex(ValueError, '跨'):
            self.library.split('BetaData/Mob/0000001.img/stand/0', self.source)

    def test_path_escape_and_missing_database(self):
        with self.assertRaises(ValueError):
            self.library.path('../outside.png')
        with self.assertRaises(ValueError):
            MonsterLibrary(self.root / 'missing').search('蜗牛')
        self.assertFalse((self.root / 'missing').exists())

    def test_import_preserves_alpha_trims_and_deduplicates(self):
        frames = self.library.frames(self.monster)
        self.assertEqual(import_frames(frames, self.monster, self.roots, ''), (1, 1))
        self.assertEqual(import_frames(frames, self.monster, self.roots, ''), (0, 2))
        paths = list(self.roots.monster.glob('*.png'))
        self.assertEqual(len(paths), 1)
        with Image.open(paths[0]) as image:
            self.assertEqual(image.mode, 'RGBA')
            self.assertEqual(image.size, (6, 6))
        self.assertEqual(read_frame(self.png).size, (6, 6))
        self.assertFalse(list(self.roots.monster.glob('*.tmp')))

    def test_empty_frame_rejected_before_any_import(self):
        blank = self.root / 'blank.png'
        Image.new('RGBA', (10,10)).save(blank)
        frames = self.library.frames(self.monster) + [MonsterFrame(self.source, 'stand/1', blank)]
        with self.assertRaises(ValueError):
            import_frames(frames, self.monster, self.roots, '')
        self.assertFalse(self.roots.monster.exists())

    def test_local_templates_load_at_one_point_five_without_rewriting_or_accumulating(self):
        import numpy as np
        from mbv.vision import load_templates
        import_frames(self.library.frames(self.monster), self.monster, self.roots, '')
        path = next(self.roots.monster.glob('*.png'))
        before = path.read_bytes()
        self.png.replace(self.roots.monster / 'captured.png')
        for _ in range(2):
            templates = {t.name: t for t in load_templates(self.roots.monster)}
            local = templates[path.name]
            self.assertEqual(local.image.shape[:2], (9, 9))
            self.assertEqual(local.foreground_mask.shape, (9, 9))
            self.assertEqual(templates['captured.png'].image.shape[:2], (10, 10))
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue(np.all(local.foreground_mask == 255))

    def test_imported_material_uses_existing_manager_and_recoverable_delete(self):
        frames = self.library.frames(self.monster)
        import_frames(frames, self.monster, self.roots, '')
        items = list_template_items('monster', roots=self.roots)
        self.assertEqual(len(items), 1)
        deleted = trash_template('monster', items[0].filename, roots=self.roots, trash_root=self.root / 'trash')
        self.assertTrue(deleted.exists())
        self.assertEqual(list_template_items('monster', roots=self.roots), [])
        self.assertTrue(self.png.exists())

    def test_import_limit_and_collision_preserve_existing_files(self):
        frame = self.library.frames(self.monster)[0]
        with self.assertRaises(ValueError):
            import_frames([frame] * 61, self.monster, self.roots, '')
        import_frames([frame], self.monster, self.roots, '')
        path = next(self.roots.monster.glob('*.png'))
        path.write_bytes(b'personal file')
        with self.assertRaisesRegex(ValueError, '冲突'):
            import_frames([frame], self.monster, self.roots, '')
        self.assertEqual(path.read_bytes(), b'personal file')

    def test_hidden_dialog_search_preview_import_and_close(self):
        import tkinter as tk
        from mbv.local_monster_dialog import LocalMonsterDialog
        root = tk.Tk()
        root.withdraw()
        native_toplevel = tk.Toplevel

        def hidden_toplevel(parent):
            dialog = native_toplevel(parent)
            dialog.withdraw()
            return dialog

        def pump(dialog):
            deadline = time.monotonic() + 5
            while dialog.future is not None and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            self.assertIsNone(dialog.future, dialog.status.get())

        dialog = None
        try:
            with patch('mbv.local_monster_dialog.tk.Toplevel', side_effect=hidden_toplevel):
                dialog = LocalMonsterDialog(root, self.roots, '', {'root': str(self.root)}, MagicMock(), MagicMock())
            dialog.query.set('蜗牛')
            dialog.search()
            pump(dialog)
            self.assertEqual(dialog.monsters, [self.monster])
            dialog.results.selection_set(0)
            dialog.select_monster()
            pump(dialog)
            self.assertIsNotNone(dialog.photo)
            dialog.add()
            pump(dialog)
            self.assertIn('添加 1 帧', dialog.status.get())
            self.assertEqual(len(list(self.roots.monster.glob('*.png'))), 1)
            self.assertEqual(root.state(), 'withdrawn')
            self.assertEqual(dialog.dialog.state(), 'withdrawn')
            dialog.close()
            self.assertTrue(dialog.closed)
        finally:
            if dialog is not None and not dialog.closed:
                if dialog.future is not None:
                    pump(dialog)
                dialog.close()
            root.destroy()


class LocalMonsterPanelTests(unittest.TestCase):
    def test_classic_profile_is_not_imported(self):
        from mbv.panel import ControlPanel
        panel = ControlPanel.__new__(ControlPanel)
        panel.busy = panel._closing = panel._stopping_session = False
        panel.config_path = Path('example.json')
        panel.root = MagicMock()
        panel._run_tool = MagicMock()
        with patch('mbv.panel.load_config', return_value={'profile':'classic'}), patch('mbv.panel.messagebox.showinfo'):
            panel._import_local_monsters()
        panel._run_tool.assert_not_called()

    def test_newmaple_import_requires_no_window(self):
        from mbv.panel import ControlPanel
        panel = ControlPanel.__new__(ControlPanel)
        panel.busy = panel._closing = panel._stopping_session = False
        panel.config_path = Path('example.json')
        panel._selected_monster_category = MagicMock(return_value='test')
        panel._run_tool = MagicMock()
        with patch('mbv.panel.load_config', return_value={'profile':'newmaple'}):
            panel._import_local_monsters()
        self.assertFalse(panel._run_tool.call_args.kwargs['requires_window'])
