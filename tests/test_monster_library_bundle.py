from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from mbv.local_monsters import (
    DEFAULT_LIBRARY, LIBRARY_RELATIVE, LocalMonster, MonsterLibrary,
    library_root_setting, read_frame, resolve_library_root,
)
from tools.build_monster_library import build_library


class LibraryDefaultTests(unittest.TestCase):
    def test_default_and_legacy_directory_use_bundle(self):
        for value in (None, '', 'D:/NewMaple_Extracted', 'd:\\newmaple_extracted\\'):
            with self.subTest(value=value):
                self.assertEqual(resolve_library_root(value), DEFAULT_LIBRARY.resolve())

    def test_relative_root_follows_project_not_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / 'relocated-project'
            bundled = project / LIBRARY_RELATIVE
            custom = Path(temporary) / 'custom-library'
            with patch('mbv.local_monsters.ROOT', project), patch('mbv.local_monsters.DEFAULT_LIBRARY', bundled):
                self.assertEqual(resolve_library_root(), bundled.resolve())
                self.assertEqual(resolve_library_root(LIBRARY_RELATIVE), bundled.resolve())
                self.assertEqual(library_root_setting(bundled), 'resources/monster_library')
                self.assertEqual(resolve_library_root(custom), custom.resolve())
                self.assertEqual(library_root_setting(custom), str(custom.resolve()))

    def test_shipped_default_can_search_and_read_without_source_library(self):
        library = MonsterLibrary(resolve_library_root())
        monsters = library.search('怪猫')
        self.assertTrue(monsters)
        frames = library.frames(monsters[0])
        self.assertTrue(frames)
        self.assertTrue(all(frame.path.is_relative_to(DEFAULT_LIBRARY.resolve()) for frame in frames))
        self.assertEqual(read_frame(frames[0].path).mode, 'RGBA')
        with library.connect() as con:
            self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(con.execute('SELECT DISTINCT category FROM names').fetchall(), [('Mob',)])


class LibraryBundleBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.output = self.root / 'output'
        self.source.mkdir()
        with closing(sqlite3.connect(self.source / 'catalog.sqlite')) as con:
            con.executescript('CREATE TABLE files(source TEXT PRIMARY KEY,metadata TEXT);'
                              'CREATE TABLE names(dataset TEXT,category TEXT,resource_id TEXT,name TEXT);')
            con.executemany('INSERT INTO names VALUES(?,?,?,?)', [
                ('Data', 'Mob', '1', '蜗牛'), ('Data', 'Mob', '2', '引用蜗牛'),
                ('Data', 'Npc', '1', '不应发布'), ('Data', 'Mob', '9', '只有名称'),
                ('BetaData', 'Mob', '1', '测试蜗牛'),
            ])
            con.commit()
        Image.new('RGBA', (8, 8), (50, 100, 150, 200)).save(self.source / 'frame.png')
        Image.new('RGBA', (8, 8), (150, 100, 50, 255)).save(self.source / 'attack.png')
        (self.source / 'unrelated.mp3').write_bytes(b'unrelated')
        self.canvas = {'type': 'canvas', 'file': 'frame.png', 'children': {}}
        self.add_tree('Data/Mob/0000001.img', {
            'stand': {'children': {'0': self.canvas}},
            'move': {'type': 'uol', 'value': 'stand'},
            'attack1': {'children': {'0': {'type': 'canvas', 'file': 'attack.png'}}},
            'die1': {'children': {'0': self.canvas}},
            'info': {'children': {'personal': {'value': 'must not copy'}}},
        })
        self.add_tree('Data/Mob/0000002.img', {'info': {'children': {'link': {'value': '1'}}}})
        self.add_tree('BetaData/Mob/0000001.img', {'fly': {'children': {'0': self.canvas}}})
        self.add_tree('Data/Map/Map1.img', {'stand': {'children': {'0': self.canvas}}})
        self.add_tree('Data/Npc/0000001.img', {'stand': {'children': {'0': self.canvas}}})

    def add_tree(self, source, children):
        name = source.replace('/', '_') + '.json'
        (self.source / name).write_text(json.dumps({'private_path': str(self.root),
                                                  'tree': {'type': 'property', 'children': children}}), encoding='utf-8')
        with closing(sqlite3.connect(self.source / 'catalog.sqlite')) as con:
            con.execute('INSERT INTO files VALUES(?,?)', (source, name))
            con.commit()

    def test_bundle_is_self_contained_deduplicated_and_monster_only(self):
        before = {p.name: p.read_bytes() for p in self.source.iterdir()}
        report = build_library(self.source, self.output)
        self.assertEqual((report['monsters'], report['frames'], report['unique_images']), (3, 5, 1))
        self.assertEqual(report['datasets'], {'BetaData': 1, 'Data': 2})
        self.assertEqual(report['skipped'], [])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.source.iterdir()})
        # Moving away the source proves references do not secretly depend on it.
        self.source.rename(self.root / 'source-moved')
        library = MonsterLibrary(self.output)
        self.assertEqual(len(library.search('蜗牛')), 2)
        self.assertEqual(library.search('只有名称'), [])
        self.assertEqual(library.search('不应发布'), [])
        self.assertEqual(library.search('测试蜗牛', 'BetaData'), [LocalMonster('BetaData', '1', '测试蜗牛')])
        frames = library.frames(LocalMonster('Data', '2', '引用蜗牛'))
        self.assertEqual(len(frames), 2)
        payload = frames[0].path.read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), frames[0].path.stem)
        self.assertEqual(payload, before['frame.png'])
        self.assertEqual(report['image_bytes'], len(payload))
        self.assertTrue(all(frame.path == frames[0].path for frame in frames))
        self.assertEqual(read_frame(frames[0].path).getchannel('A').getextrema(), (200, 200))
        for path in (self.output / 'metadata').rglob('*.json'):
            content = path.read_text(encoding='utf-8')
            self.assertNotIn('private_path', content)
            self.assertNotIn('personal', content)
            self.assertNotIn('attack1', content)
            self.assertNotIn('die1', content)
        with library.connect() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM files').fetchone()[0], 3)
            self.assertEqual(con.execute('SELECT DISTINCT category FROM names').fetchall(), [('Mob',)])
        self.assertEqual({p.suffix for p in self.output.rglob('*') if p.is_file()}, {'.json', '.sqlite', '.png'})

    def test_existing_destination_and_nested_source_are_rejected(self):
        self.output.mkdir()
        marker = self.output / 'personal.txt'
        marker.write_text('keep', encoding='utf-8')
        with self.assertRaises(ValueError):
            build_library(self.source, self.output)
        self.assertEqual(marker.read_text(encoding='utf-8'), 'keep')
        with self.assertRaises(ValueError):
            build_library(self.source, self.source / 'nested')
        self.assertFalse((self.source / 'nested').exists())

    def test_invalid_monster_is_reported_without_copying_partial_files(self):
        self.add_tree('Data/Mob/0000004.img', {'stand': {'children': {
            '0': self.canvas, '1': {'type': 'canvas', 'file': '../outside.png'},
        }}})
        report = build_library(self.source, self.output)
        self.assertEqual(report['skipped'], [{'source': 'Data/Mob/0000004.img', 'reason': 'ValueError'}])
        self.assertEqual(report['unique_images'], 1)
        self.assertEqual(MonsterLibrary(self.output).search('4'), [])


if __name__ == '__main__':
    unittest.main()
