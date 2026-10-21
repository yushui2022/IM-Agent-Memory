#!/usr/bin/env python3
"""跑全部测试： python tests/run_all.py"""
from __future__ import annotations

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

MODULES = ["test_imem", "test_outbox", "test_drafter"]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    total_passed = total_failed = 0
    for name in MODULES:
        mod = load(name)
        tests = [(k, v) for k, v in sorted(vars(mod).items())
                 if k.startswith("test_") and callable(v)]
        print(f"\n=== {name} ({len(tests)} 个) ===")
        for tname, fn in tests:
            try:
                fn()
                print(f"  ok    {tname}")
                total_passed += 1
            except Exception as exc:
                print(f"  FAIL  {tname}: {type(exc).__name__}: {exc}")
                total_failed += 1
    print(f"\n{'=' * 40}")
    print(f"合计 {total_passed} passed, {total_failed} failed")
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
