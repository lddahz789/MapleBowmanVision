"""Read-only adapter for the exported NewMaple catalog; no external code execution."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import posixpath
import re
import sqlite3
import tempfile

from PIL import Image

from mbv.template_store import TemplateRoots, template_directory
from mbv.paths import ROOT

LIBRARY_RELATIVE = Path("resources/monster_library")
DEFAULT_LIBRARY = ROOT / LIBRARY_RELATIVE


def resolve_library_root(value: str | Path | None = None) -> Path:
    """项目内库为默认值；旧默认目录迁移，显式选择的其他目录仍保留。"""
    if not value or str(value).replace("\\", "/").rstrip("/").casefold() == "d:/newmaple_extracted":
        return DEFAULT_LIBRARY.resolve()
    path = Path(value)
    return (path if path.is_absolute() else ROOT / path).resolve()


def library_root_setting(path: Path) -> str:
    resolved = path.resolve()
    return LIBRARY_RELATIVE.as_posix() if resolved == DEFAULT_LIBRARY.resolve() else str(resolved)


@dataclass(frozen=True)
class LocalMonster:
    dataset: str
    resource_id: str
    name: str


@dataclass(frozen=True)
class MonsterFrame:
    source: str
    node: str
    path: Path


class MonsterLibrary:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self._trees: dict[str, dict] = {}

    def path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("资源索引指向库目录之外，已拒绝读取")
        return path

    @contextmanager
    def connect(self):
        database = self.path("catalog.sqlite")
        if not database.is_file():
            raise ValueError("此目录没有 catalog.sqlite，请选择解包资源库根目录")
        con = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)
        try:
            yield con
        finally:
            con.close()

    def search(self, query: str, dataset: str = "Data") -> list[LocalMonster]:
        if dataset not in {"Data", "BetaData"}:
            raise ValueError("请选择正式服或测试服数据")
        query = query.strip()
        if not query:
            return []
        if len(query) > 100:
            raise ValueError("搜索词过长")
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rid = str(int(query)) if query.isascii() and query.isdecimal() else query
        with self.connect() as con:
            rows = con.execute(
                "SELECT DISTINCT dataset,resource_id,name FROM names n "
                "WHERE category='Mob' AND dataset=? AND (name LIKE ? ESCAPE '\\' OR resource_id=?) "
                "AND EXISTS (SELECT 1 FROM files f WHERE f.source IN "
                "(n.dataset || '/Mob/' || printf('%07d', CAST(n.resource_id AS INTEGER)) || '.img', "
                "n.dataset || '/Mob/' || n.resource_id || '.img')) "
                "ORDER BY CASE WHEN name=? THEN 0 ELSE 1 END, name, resource_id LIMIT 100",
                (dataset, f"%{escaped}%", rid, query),
            ).fetchall()
        return [LocalMonster(*row) for row in rows]

    def source(self, dataset: str, resource_id: str) -> str:
        if dataset not in {"Data", "BetaData"} or not re.fullmatch(r"[0-9]{1,10}", resource_id):
            raise ValueError("怪物资源标识无效")
        with self.connect() as con:
            for number in (f"{int(resource_id):07d}", str(int(resource_id))):
                candidate = f"{dataset}/Mob/{number}.img"
                if con.execute("SELECT 1 FROM files WHERE source=?", (candidate,)).fetchone():
                    return candidate
        raise ValueError("该数据集只有名称、没有对应怪物图片；不会改用另一数据集")

    def tree(self, source: str) -> dict:
        if not re.fullmatch(r"(?:Data|BetaData)/Mob/[0-9]+\.img", source):
            raise ValueError("引用不属于怪物资源，已拒绝")
        if source in self._trees:
            return self._trees[source]
        with self.connect() as con:
            row = con.execute("SELECT metadata FROM files WHERE source=?", (source,)).fetchone()
        if not row:
            raise ValueError(f"缺失引用：{source}")
        path = self.path(row[0])
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("怪物元数据过大")
        tree = json.loads(path.read_text(encoding="utf-8"))["tree"]
        if len(self._trees) >= 32:
            self._trees.clear()
        self._trees[source] = tree
        return tree

    def locate(self, source: str, node: str, seen: tuple = ()) -> tuple[str, str, dict]:
        address = (source, node)
        if address in seen or len(seen) >= 32:
            raise ValueError("资源引用循环或层级过深")
        current = self.tree(source)
        parts = node.split("/") if node else []
        walked: list[str] = []
        for index, part in enumerate(parts):
            current = current["children"][part]
            walked.append(part)
            if current.get("type") == "uol":
                dest = posixpath.normpath(posixpath.join(source, *walked[:-1], current["value"]))
                src, sub = self.split(dest, source)
                return self.locate(src, "/".join(filter(None, (sub, *parts[index + 1:]))), seen + (address,))
        return source, node, current

    @staticmethod
    def split(address: str, original: str) -> tuple[str, str]:
        parts = address.replace("\\", "/").split("/")
        end = next(i for i, value in enumerate(parts) if value.endswith(".img"))
        source = "/".join(parts[:end + 1])
        if parts[0] != original.split("/")[0]:
            raise ValueError("跨正式服/测试服引用已拒绝")
        return source, "/".join(parts[end + 1:])

    def image(self, source: str, node: str, seen: tuple = ()) -> Path:
        address = (source, node)
        if address in seen or len(seen) >= 32:
            raise ValueError("图片引用循环或层级过深")
        source, node, item = self.locate(source, node)
        children = item.get("children", {})
        if "_inlink" in children:
            return self.image(source, children["_inlink"]["value"], seen + (address,))
        if "_outlink" in children:
            dest = children["_outlink"]["value"].replace("\\", "/")
            if not dest.startswith(("Data/", "BetaData/")):
                dest = source.split("/")[0] + "/" + dest
            src, sub = self.split(dest, source)
            return self.image(src, sub, seen + (address,))
        if item["type"] != "canvas":
            raise ValueError("该节点不是图片")
        return self.path(item["file"])

    def frames(self, monster: LocalMonster) -> list[MonsterFrame]:
        source = self.source(monster.dataset, monster.resource_id)
        seen = set()
        while True:
            if source in seen or len(seen) >= 32:
                raise ValueError("怪物整体引用循环")
            seen.add(source)
            tree = self.tree(source)
            children = tree.get("children", {})
            actions = [key for key in children if re.fullmatch(r"(?:stand|move|fly)[0-9]*", key)]
            if actions:
                break
            link = children.get("info", {}).get("children", {}).get("link", {}).get("value")
            if link is None:
                raise ValueError("该怪物没有可用的站立、移动或飞行动作帧")
            source = self.source(monster.dataset, str(link))
        result = []
        for action in sorted(actions):
            _, _, item = self.locate(source, action)
            for number in sorted(item.get("children", {}), key=lambda text: (not text.isdecimal(), text.zfill(8))):
                if not number.isdecimal():
                    continue
                node = f"{action}/{number}"
                result.append(MonsterFrame(source, node, self.image(source, node)))
                if len(result) >= 120:
                    return result
        return result


def read_frame(path: Path) -> Image.Image:
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("单帧文件过大")
    with Image.open(path) as image:
        if image.format != "PNG" or image.width * image.height > 4_000_000:
            raise ValueError("只支持合理尺寸的 PNG")
        rgba = image.convert("RGBA")
    bounds = rgba.getchannel("A").getbbox()
    if bounds is None or bounds[2] - bounds[0] < 3 or bounds[3] - bounds[1] < 3:
        raise ValueError("空白或占位图片不能作为识别素材")
    return rgba.crop(bounds)


def import_frames(frames: list[MonsterFrame], monster: LocalMonster, roots: TemplateRoots,
                  category: str) -> tuple[int, int]:
    if not frames or len(frames) > 60:
        raise ValueError("每次请选择 1–60 帧，避免大量模板拖慢识别")
    prepared: dict[str, bytes] = {}
    label = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', monster.name)[:24].strip(' .') or 'monster'
    number = re.sub(r'[^0-9]', '', monster.resource_id)[:10]
    for frame in frames:
        image = read_frame(frame.path)
        data = io.BytesIO()
        image.save(data, format="PNG")
        payload = data.getvalue()
        digest = hashlib.sha256(payload).hexdigest()
        prepared[f"local-{label}-{number}-{digest}.png"] = payload
        if sum(map(len, prepared.values())) > 64 * 1024 * 1024:
            raise ValueError("此次导入图片总量过大，请减少帧数")
    destination = template_directory("monster", category, roots=roots, create=True)
    added = 0
    skipped = len(frames) - len(prepared)
    for name, payload in prepared.items():
        path = destination / name
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
            os.link(temporary, path)
            added += 1
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise ValueError("同名素材内容冲突，未覆盖已有文件")
            skipped += 1
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return added, skipped
