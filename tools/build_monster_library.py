"""Build a self-contained, monster-template-only catalog from an exported library."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mbv.local_monsters import LocalMonster, MonsterLibrary, read_frame


def build_library(source: Path, destination: Path) -> dict:
    source, destination = source.resolve(), destination.resolve()
    if destination.exists():
        raise ValueError("输出目录已存在，请使用新的空目录；不会覆盖已有资源")
    if destination == source or destination.is_relative_to(source):
        raise ValueError("输出目录不能位于源资源库内")
    library = MonsterLibrary(source)
    with library.connect() as con:
        sources = [row[0] for row in con.execute("SELECT source FROM files ORDER BY source")
                   if re.fullmatch(r"(?:Data|BetaData)/Mob/[0-9]+\.img", row[0])]
        names = {}
        for dataset, rid, name in con.execute(
            "SELECT DISTINCT dataset,resource_id,name FROM names WHERE category='Mob' ORDER BY dataset,resource_id,name"
        ):
            if dataset in {"Data", "BetaData"} and str(rid).isdecimal():
                names.setdefault((dataset, str(int(rid))), []).append(name)
    destination.mkdir(parents=True)
    con = sqlite3.connect(destination / "catalog.sqlite")
    con.executescript(
        "CREATE TABLE files(source TEXT PRIMARY KEY,metadata TEXT NOT NULL);"
        "CREATE TABLE names(dataset TEXT,category TEXT,resource_id TEXT,name TEXT);"
        "CREATE INDEX names_lookup ON names(dataset,category,resource_id);"
    )
    report = {"schema_version": 1, "scope": "Mob stand/move/fly frames only", "monsters": 0,
              "frames": 0, "unique_images": 0, "image_bytes": 0, "datasets": {}, "skipped": []}
    written = set()
    try:
        for index, source_name in enumerate(sources):
            dataset, _, filename = source_name.split("/")
            rid = str(int(filename[:-4]))
            monster = LocalMonster(dataset, rid, "")
            try:
                frames = library.frames(monster)
                if not frames:
                    raise ValueError("no frames")
                prepared = []
                for frame in frames:
                    if not re.fullmatch(r"(?:stand|move|fly)[0-9]*/[0-9]+", frame.node):
                        raise ValueError("non-recognition action")
                    # Resolve all references with MonsterLibrary before copying; never copy scripts or unrelated trees.
                    read_frame(frame.path)
                    payload = frame.path.read_bytes()
                    digest = hashlib.sha256(payload).hexdigest()
                    relative = f"images/{digest[:2]}/{digest}.png"
                    prepared.append((frame.node, relative, digest, payload))
            except (ValueError, OSError, KeyError, IndexError, sqlite3.Error) as exc:
                # Do not leak local source paths or exported metadata into the public manifest.
                report["skipped"].append({"source": source_name, "reason": type(exc).__name__})
                continue
            tree = {"type": "property", "children": {}}
            for node, relative, digest, payload in prepared:
                if digest not in written:
                    path = destination / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(payload)
                    written.add(digest)
                    report["image_bytes"] += len(payload)
                action, number = node.split("/")
                action_tree = tree["children"].setdefault(action, {"type": "property", "children": {}})
                action_tree["children"][number] = {"type": "canvas", "file": relative, "children": {}}
            metadata = f"metadata/{dataset}/{filename}.json"
            target = destination / metadata
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"schema_version": 1, "source": source_name, "tree": tree},
                                         ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
            con.execute("INSERT INTO files VALUES(?,?)", (source_name, metadata))
            for name in names.get((dataset, rid), [f"怪物 {rid}"]):
                con.execute("INSERT INTO names VALUES(?,?,?,?)", (dataset, "Mob", rid, name))
            report["monsters"] += 1
            report["frames"] += len(prepared)
            report["datasets"][dataset] = report["datasets"].get(dataset, 0) + 1
            if (index + 1) % 200 == 0:
                print(f"Processed {index + 1}/{len(sources)}", flush=True)
        con.commit()
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("生成的怪物索引校验失败")
    finally:
        con.close()
    report["unique_images"] = len(written)
    (destination / "manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    report = build_library(args.source, args.destination)
    print(json.dumps({k: v for k, v in report.items() if k != "skipped"}, ensure_ascii=False))
    print("Skipped unsupported or incomplete monsters:", len(report["skipped"]))
