from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import shutil

from mbv.paths import ASSET_DIR, MONSTER_FILTER_ASSET_DIR, TEMPLATE_TRASH_DIR


UNCATEGORIZED = ""
UNCATEGORIZED_LABEL = "未分类"
CATEGORY_MARKER = ".gitkeep"
_INVALID_CATEGORY_CHARS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


@dataclass(frozen=True)
class MonsterCategory:
    name: str
    monster_count: int
    filter_count: int

    @property
    def label(self) -> str:
        return self.name or UNCATEGORIZED_LABEL


@dataclass(frozen=True)
class MonsterTemplateItem:
    category: str
    kind: str
    filename: str
    path: Path


def validate_category_name(name: str) -> str:
    value = str(name).strip()
    if not value:
        raise ValueError("分类名称不能为空")
    if value.casefold() == UNCATEGORIZED_LABEL.casefold():
        raise ValueError(f'“{UNCATEGORIZED_LABEL}”是系统保留分类名称')
    if value.startswith("."):
        raise ValueError("分类名称不能以点号开头")
    if value in {".", ".."} or value.endswith((" ", ".")):
        raise ValueError("分类名称不能是点号，也不能以空格或点号结尾")
    if len(value) > 40:
        raise ValueError("分类名称不能超过 40 个字符")
    if any(character in _INVALID_CATEGORY_CHARS or ord(character) < 32 for character in value):
        raise ValueError('分类名称不能包含 < > : " / \\ | ? * 或控制字符')
    device_name = value.split(".", 1)[0].upper()
    if device_name in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f'“{value}”是 Windows 保留名称')
    return value


def _direct_pngs(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    try:
        return sorted(
            (path for path in directory.glob("*.png") if path.is_file()),
            key=lambda path: path.name.casefold(),
        )
    except OSError:
        return []


def _category_names(monster_root: Path, filter_root: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    for root in (monster_root, filter_root):
        if not root.is_dir():
            continue
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for path in entries:
            if path.is_dir() and not path.name.startswith("."):
                names.setdefault(path.name.casefold(), path.name)
    return names


def list_monster_categories(
    monster_root: Path = ASSET_DIR,
    filter_root: Path = MONSTER_FILTER_ASSET_DIR,
) -> list[MonsterCategory]:
    monster_root.mkdir(parents=True, exist_ok=True)
    filter_root.mkdir(parents=True, exist_ok=True)
    result = [MonsterCategory(UNCATEGORIZED, len(_direct_pngs(monster_root)), len(_direct_pngs(filter_root)))]
    names = _category_names(monster_root, filter_root)
    for name in sorted(names.values(), key=str.casefold):
        result.append(
            MonsterCategory(
                name,
                len(_direct_pngs(monster_root / name)),
                len(_direct_pngs(filter_root / name)),
            )
        )
    return result


def _existing_category_name(name: str, monster_root: Path, filter_root: Path) -> str:
    if not name:
        return UNCATEGORIZED
    value = validate_category_name(name)
    match = _category_names(monster_root, filter_root).get(value.casefold())
    if match is None:
        raise FileNotFoundError(f'怪物分类“{value}”不存在')
    return match


def _safe_category_directory(root: Path, name: str) -> Path:
    root = root.resolve()
    if not name:
        return root
    value = validate_category_name(name)
    directory = (root / value).resolve()
    if directory.parent != root:
        raise ValueError("分类目录超出模板根目录")
    return directory


def monster_template_directory(
    category: str,
    kind: str = "monster",
    *,
    monster_root: Path = ASSET_DIR,
    filter_root: Path = MONSTER_FILTER_ASSET_DIR,
    create: bool = False,
) -> Path:
    if kind not in {"monster", "filter"}:
        raise ValueError(f"未知模板类型：{kind}")
    actual = _existing_category_name(category, monster_root, filter_root) if category else UNCATEGORIZED
    root = monster_root if kind == "monster" else filter_root
    directory = _safe_category_directory(root, actual)
    if create:
        directory.mkdir(parents=True, exist_ok=True)
        if actual:
            (directory / CATEGORY_MARKER).touch(exist_ok=True)
    return directory


def create_monster_category(
    name: str,
    *,
    monster_root: Path = ASSET_DIR,
    filter_root: Path = MONSTER_FILTER_ASSET_DIR,
) -> str:
    value = validate_category_name(name)
    if value.casefold() in _category_names(monster_root, filter_root):
        raise FileExistsError(f'怪物分类“{value}”已经存在')
    created_directories: list[Path] = []
    try:
        for root in (monster_root, filter_root):
            directory = _safe_category_directory(root, value)
            directory.mkdir(parents=True, exist_ok=False)
            created_directories.append(directory)
            (directory / CATEGORY_MARKER).touch(exist_ok=False)
    except Exception:
        for directory in reversed(created_directories):
            shutil.rmtree(directory, ignore_errors=True)
        raise
    return value


def rename_monster_category(
    old_name: str,
    new_name: str,
    *,
    monster_root: Path = ASSET_DIR,
    filter_root: Path = MONSTER_FILTER_ASSET_DIR,
) -> str:
    if not old_name:
        raise ValueError(f'系统分类“{UNCATEGORIZED_LABEL}”不能重命名')
    actual = _existing_category_name(old_name, monster_root, filter_root)
    target = validate_category_name(new_name)
    if actual == target:
        return actual
    if actual.casefold() == target.casefold():
        raise ValueError("Windows 下不能只修改分类名称的大小写")
    if target.casefold() in _category_names(monster_root, filter_root):
        raise FileExistsError(f'怪物分类“{target}”已经存在')

    renamed: list[tuple[Path, Path]] = []
    try:
        for root in (monster_root, filter_root):
            source = _safe_category_directory(root, actual)
            if not source.exists():
                continue
            destination = _safe_category_directory(root, target)
            source.rename(destination)
            renamed.append((source, destination))
    except Exception:
        for source, destination in reversed(renamed):
            if destination.exists() and not source.exists():
                destination.rename(source)
        raise
    return target


def _trash_group(trash_root: Path, label: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    group = (trash_root.resolve() / f"{stamp}-{label}").resolve()
    if group.parent != trash_root.resolve():
        raise ValueError("回收目录超出模板回收站")
    group.mkdir(parents=True, exist_ok=False)
    return group


def trash_monster_category(
    name: str,
    *,
    monster_root: Path = ASSET_DIR,
    filter_root: Path = MONSTER_FILTER_ASSET_DIR,
    trash_root: Path = TEMPLATE_TRASH_DIR,
) -> Path:
    if not name:
        raise ValueError(f'系统分类“{UNCATEGORIZED_LABEL}”不能删除；可以在“管理模板”中删除其中的图片')
    actual = _existing_category_name(name, monster_root, filter_root)
    trash_root.mkdir(parents=True, exist_ok=True)
    group = _trash_group(trash_root, actual)
    moved: list[tuple[Path, Path]] = []
    try:
        for kind, root in (("monsters", monster_root), ("filters", filter_root)):
            source = _safe_category_directory(root, actual)
            if source.exists():
                destination = group / kind / actual
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                moved.append((source, destination))
    except Exception:
        for source, destination in reversed(moved):
            if destination.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(destination), str(source))
        shutil.rmtree(group, ignore_errors=True)
        raise
    return group


def list_monster_template_items(
    category: str,
    kind: str,
    *,
    monster_root: Path = ASSET_DIR,
    filter_root: Path = MONSTER_FILTER_ASSET_DIR,
) -> list[MonsterTemplateItem]:
    directory = monster_template_directory(
        category,
        kind,
        monster_root=monster_root,
        filter_root=filter_root,
    )
    return [MonsterTemplateItem(category, kind, path.name, path) for path in _direct_pngs(directory)]


def trash_monster_template(
    category: str,
    kind: str,
    filename: str,
    *,
    monster_root: Path = ASSET_DIR,
    filter_root: Path = MONSTER_FILTER_ASSET_DIR,
    trash_root: Path = TEMPLATE_TRASH_DIR,
) -> Path:
    if Path(filename).name != filename or Path(filename).suffix.casefold() != ".png":
        raise ValueError("只能删除分类中已列出的 PNG 模板")
    directory = monster_template_directory(
        category,
        kind,
        monster_root=monster_root,
        filter_root=filter_root,
    ).resolve()
    source = (directory / filename).resolve()
    if source.parent != directory or not source.is_file():
        raise FileNotFoundError(f"模板不存在：{filename}")
    trash_root.mkdir(parents=True, exist_ok=True)
    group = _trash_group(trash_root, category or UNCATEGORIZED_LABEL)
    destination = group / kind / (category or UNCATEGORIZED_LABEL) / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    return destination
