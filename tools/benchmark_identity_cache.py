from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import statistics
import subprocess
import sys
from timeit import timeit

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mbv import vision  # noqa: E402


BASELINE = "7b00cad9e71a57bb7711644603c37cf56a317993"


def baseline_verifier(revision: str):
    """只读指定本地 Git 版本，复用其未经修改的旧评分和验证函数。"""
    source = subprocess.run(
        ["git", "show", f"{revision}:mbv/vision.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        encoding="utf-8",
    ).stdout
    names = {"nameplate_identity_similarity", "verify_nameplate_identities"}
    functions = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name in names]
    if len(functions) != len(names):
        raise ValueError("基线中缺少姓名板身份校验函数")
    namespace = dict(vars(vision))
    exec(compile(ast.Module(body=functions, type_ignores=[]), "<baseline-identity>", "exec"), namespace)
    return namespace["verify_nameplate_identities"]


def run_benchmark(revision: str, iterations: int, rounds: int) -> dict:
    old_verify = baseline_verifier(revision)
    image = np.full((24, 96, 3), (180, 70, 20), dtype=np.uint8)
    cv2.putText(image, "MAPLE", (20, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (245, 245, 245), 1)
    other = np.full_like(image, (180, 70, 20))
    cv2.putText(other, "OTHER", (20, 17), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (245, 245, 245), 1)
    alpha = np.zeros((24, 96), dtype=np.uint8)
    alpha[2:-2, 4:-4] = 255
    workloads = []
    for candidate_count in (1, 8, 32):
        scene = np.tile(image, (candidate_count, 1, 1))
        for index in range(candidate_count):
            if index % 4 == 1:
                scene[index * 24:(index + 1) * 24] = np.roll(image, 1, axis=1)
            elif index % 4 == 2:
                scene[index * 24:(index + 1) * 24] = other
            elif index % 4 == 3:
                scene[index * 24:(index + 1) * 24] = (180, 70, 20)
        templates = [vision.Template("synthetic.png", image, alpha)]
        detections = [vision.Detection((0, index * 24, 96, 24), 0.9, "synthetic.png") for index in range(candidate_count)]
        old_run = lambda: old_verify(scene, detections, templates)
        cached_run = lambda: vision.verify_nameplate_identities(scene, detections, templates)
        old_result, cached_result = old_run(), cached_run()
        if old_result != cached_result:
            raise AssertionError("缓存路径与原算法输出不同")
        for _ in range(30):
            old_run()
            cached_run()
        old_times, cached_times = [], []
        for round_index in range(rounds):
            runners = ((old_run, old_times), (cached_run, cached_times))
            if round_index % 2:
                runners = tuple(reversed(runners))
            for run, times in runners:
                times.append(timeit(run, number=iterations) * 1000 / iterations)
        old_ms, cached_ms = statistics.median(old_times), statistics.median(cached_times)
        workloads.append({
            "candidates": candidate_count,
            "old_median_ms": round(old_ms, 4),
            "cached_median_ms": round(cached_ms, 4),
            "reduction_percent": round(100 * (old_ms - cached_ms) / old_ms, 1),
            "old_rounds_ms": [round(value, 4) for value in old_times],
            "cached_rounds_ms": [round(value, 4) for value in cached_times],
            "scores": [item.identity_score for item in cached_result],
            "cache_array_bytes": templates[0].nameplate_identity_features()[0].nbytes,
        })
    return {
        "baseline": revision,
        "scope": "仅合成姓名板候选身份校验整批耗时，不代表游戏 FPS",
        "opencv": cv2.__version__,
        "numpy": np.__version__,
        "iterations_per_round": iterations,
        "alternating_rounds": rounds,
        "workloads": workloads,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="交替对比静态姓名板字形缓存与本地 Git 基线；不读取个人素材或运行游戏。")
    parser.add_argument("--baseline", default=BASELINE)
    parser.add_argument("--iterations", type=int, default=300)
    parser.add_argument("--rounds", type=int, default=7)
    args = parser.parse_args()
    if args.iterations < 1 or args.rounds < 1:
        parser.error("iterations 和 rounds 必须大于零")
    print(json.dumps(run_benchmark(args.baseline, args.iterations, args.rounds), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
