"""Запускает все tests/browser/test_b*.py как отдельные процессы,
печатает сводку, код выхода != 0 при падении хотя бы одного сценария.

Тот же принцип, что и у остальных tests/*.py (самостоятельные скрипты,
не pytest — см. AGENTS.md → «Команды»). Требует Chromium/Playwright
(PLAYWRIGHT_BROWSERS_PATH, см. README) — mosquitto НЕ требуется:
браузерные сценарии не используют MQTT, только HTTP и прямые записи в
БД через harness.py.

Запуск:
    python tests/browser/run.py
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    scripts = sorted(glob.glob(os.path.join(HERE, "test_b*.py")))
    if not scripts:
        print("Нет ни одного tests/browser/test_b*.py")
        return 1

    results = []
    for script in scripts:
        name = os.path.basename(script)
        print(f"=== {name} ===", flush=True)
        proc = subprocess.run([sys.executable, script], cwd=HERE)
        results.append((name, proc.returncode))
        print()

    print("--- Сводка ---")
    for name, rc in results:
        print(f"{'OK  ' if rc == 0 else 'FAIL'} {name} (код {rc})")

    failed = [n for n, rc in results if rc != 0]
    if failed:
        print(f"\n{len(failed)} из {len(results)} браузерных сценариев упали")
        return 1
    print(f"\nВсе {len(results)} браузерных сценария(ев) зелёные")
    return 0


if __name__ == "__main__":
    sys.exit(main())
