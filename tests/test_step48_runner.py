"""Шаг 48 (партия 10, этап D, F2): единый раннер тестов tests/run_all.py
(docs/TZ-batch10-reliability-and-load.md §4).

До этой партии ci.yml перечислял все тестовые файлы вручную (87 строк
на два job'а), и это уже приводило к реальному пропуску: test_step40 и
test_step41 (партия 7) не были зарегистрированы ни в одном job'е до
партии 8. run_all.py ходит по диску сам (glob), поэтому здесь
проверяется именно это — а не то, что каждый отдельный тест внутри
проходит (это уже проверяют сами тесты).

Самостоятельный скрипт (не pytest):
    python tests/test_step48_runner.py
"""

from __future__ import annotations

import glob
import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

RUN_ALL = os.path.join(REPO_ROOT, "tests", "run_all.py")
REAL_GLOB_PATTERN = os.path.join(REPO_ROOT, "tests", "test_step*.py")


def _load_run_all_module():
    """Импортирует tests/run_all.py как модуль напрямую по пути — папка
    tests/ не пакет (в ней нет __init__.py), обычный import не сработает."""
    spec = importlib.util.spec_from_file_location("wbem_run_all", RUN_ALL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bash_python_syntax_and_help():
    r = subprocess.run([sys.executable, "-m", "py_compile", RUN_ALL],
                        capture_output=True, text=True)
    assert r.returncode == 0, f"tests/run_all.py не компилируется: {r.stderr}"
    r = subprocess.run([sys.executable, RUN_ALL, "--help"],
                        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    print("[OK] run_all.py компилируется и отвечает на --help")


# Цель для --only в проверках ниже — ЧУЖОЙ, быстрый и не имеющий побочных
# эффектов файл (НЕ test_step48_runner.py: --only test_step48_runner.py
# запустил бы run_all.py, который запустил бы этот же файл, который внутри
# СЕБЯ снова запускает run_all.py — саморазмножающаяся рекурсия вместо
# теста; поймано прямо на этом шаге при первом прогоне).
ONLY_TARGET = "test_step45_package_data.py"


def test_runner_reports_same_count_as_real_glob():
    """Раннер должен найти РОВНО столько файлов, сколько их реально на
    диске (та самая защита от "нечаянно отфильтровали" из истории
    test_step40/41). Гоняем реальный run_all.py на реальном каталоге
    tests/, но с --only, сузив ФАКТИЧЕСКИЙ запуск до одного чужого
    быстрого файла — иначе прогон ждал бы весь набор целиком (а
    --only на себя самого — саморазмножающаяся рекурсия, см. ONLY_TARGET)."""
    on_disk = len(glob.glob(REAL_GLOB_PATTERN))
    assert on_disk >= 40, f"подозрительно мало файлов на диске: {on_disk}"

    r = subprocess.run(
        [sys.executable, RUN_ALL, "--only", ONLY_TARGET],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"Найдено файлов tests/test_step*.py: {on_disk}" in r.stdout, (
        f"раннер должен был отчитаться о {on_disk} файлах; вывод:\n{r.stdout}")
    print(f"[OK] run_all.py находит все {on_disk} файлов на диске "
          "(сверено с независимым glob() в тесте)")


def test_only_narrows_selection_and_bad_pattern_is_error():
    r = subprocess.run(
        [sys.executable, RUN_ALL, "--only", ONLY_TARGET],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "выбрано: 1" in r.stdout, r.stdout
    assert f"PASS {ONLY_TARGET}" in r.stdout, r.stdout

    r = subprocess.run(
        [sys.executable, RUN_ALL, "--only", "no-such-pattern-xyz*.py"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
    assert r.returncode != 0, (
        "--only без единого совпадения должен быть ошибкой (вероятная опечатка)")
    print("[OK] --only сужает выборку; несуществующий шаблон -> ошибка")


def test_failing_file_gives_nonzero_exit_in_isolated_copy():
    """Настоящий негативный сценарий раннера нельзя проверить на боевом
    каталоге tests/ (там намеренно нет падающих файлов) — поднимаем
    изолированную копию структуры в temp: свой run_all.py (TESTS_DIR
    вычисляется от расположения файла) + один заведомо падающий тест +
    один проходящий, и убеждаемся, что раннер отличает их правильно."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_tests = os.path.join(tmp, "tests")
        os.makedirs(tmp_tests)
        shutil.copy2(RUN_ALL, os.path.join(tmp_tests, "run_all.py"))

        with open(os.path.join(tmp_tests, "test_step90_ok.py"), "w") as f:
            f.write("import sys\nprint('ok')\nsys.exit(0)\n")
        with open(os.path.join(tmp_tests, "test_step91_broken.py"), "w") as f:
            f.write("import sys\nprint('kaboom')\nsys.exit(1)\n")

        r = subprocess.run(
            [sys.executable, os.path.join(tmp_tests, "run_all.py")],
            cwd=tmp, capture_output=True, text=True, timeout=60)
        assert r.returncode != 0, (
            f"раннер должен вернуть ненулевой код при провале хотя бы "
            f"одного файла:\n{r.stdout}")
        assert "Найдено файлов tests/test_step*.py: 2" in r.stdout, r.stdout
        assert "PASS test_step90_ok.py" in r.stdout, r.stdout
        assert "FAIL test_step91_broken.py" in r.stdout, r.stdout
        print("[OK] один падающий файл из двух -> ненулевой код выхода, "
              "проходящий файл при этом не пострадал")


def test_mosquitto_detection_by_name_and_skip_flag():
    """Раннер узнаёт тесты, которым нужен mosquitto, по ИМЕНИ файла
    (*_daemon.py или точно *_e2e.py) — не по содержимому. Проверяем
    заодно, что это не ловит файлы, которые просто упоминают
    "mosquitto"/"1883" текстом (конфиг-пример, комментарий), и не
    путает "*_e2e.py" с "*_e2e_что-то-ещё.py"."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_tests = os.path.join(tmp, "tests")
        os.makedirs(tmp_tests)
        shutil.copy2(RUN_ALL, os.path.join(tmp_tests, "run_all.py"))

        # Должен быть распознан и пропущен -- имя оканчивается на _daemon.py.
        with open(os.path.join(tmp_tests, "test_step92_fake_daemon.py"), "w") as f:
            f.write("import sys\nsys.exit(1)  # если бы реально запустился -- упал бы\n")
        # Должен быть распознан и пропущен -- имя оканчивается ТОЧНО на _e2e.py.
        with open(os.path.join(tmp_tests, "test_step93_fake_e2e.py"), "w") as f:
            f.write("import sys\nsys.exit(1)\n")
        # НЕ должен быть распознан как нуждающийся в брокере: "e2e" в
        # имени есть, но не как точный суффикс "_e2e.py" (тот самый
        # случай test_step34_e2e_ui_creation_path.py) -- и содержимое
        # текстом упоминает mosquitto/1883, что раньше (наивная проверка
        # по содержимому) ложно цепляло бы этот файл.
        with open(os.path.join(tmp_tests, "test_step94_e2e_but_no_broker.py"), "w") as f:
            f.write(
                "# просто упоминание: mosquitto, port 1883 -- не значит, что "
                "этому тесту реально нужен брокер\n"
                "import sys\nprint('ok')\nsys.exit(0)\n")

        r = subprocess.run(
            [sys.executable, os.path.join(tmp_tests, "run_all.py"),
             "--skip-mosquitto"],
            cwd=tmp, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "SKIP test_step92_fake_daemon.py" in r.stdout, r.stdout
        assert "SKIP test_step93_fake_e2e.py" in r.stdout, r.stdout
        assert "PASS test_step94_e2e_but_no_broker.py" in r.stdout, (
            "файл с 'e2e' в имени, но не с точным суффиксом _e2e.py, и с "
            "упоминанием mosquitto/1883 текстом -- не должен пропускаться:\n"
            + r.stdout)

        # Без флага, брокера НЕТ: адрес проверки направлен на заведомо
        # закрытый порт через WBEM_TEST_MQTT_PROBE. Раньше здесь молча
        # предполагалось, что на 127.0.0.1:1883 никого нет, -- а в job
        # `test` ci.yml mosquitto поднимается ДО раннера, и этот тест
        # валил весь job на всех версиях Python (найдено 09.10.2026).
        r2 = subprocess.run(
            [sys.executable, os.path.join(tmp_tests, "run_all.py")],
            cwd=tmp, capture_output=True, text=True, timeout=30,
            env=_env_with_probe(f"127.0.0.1:{_closed_port()}"))
        assert r2.returncode == 0, r2.stdout + r2.stderr
        assert "SKIP test_step92_fake_daemon.py" in r2.stdout, r2.stdout
        assert "SKIP test_step93_fake_e2e.py" in r2.stdout, r2.stdout
        assert "PASS test_step94_e2e_but_no_broker.py" in r2.stdout, r2.stdout

        # Парная проверка: без флага, брокер ЕСТЬ (слушающий сокет на
        # свободном порту). Раннер обязан попытаться запустить
        # *_daemon.py/*_e2e.py; фальшивые падают -- код выхода != 0. Без
        # этой половины тест выше прошёл бы и у раннера, который всегда
        # всё пропускает.
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(4)
            port = listener.getsockname()[1]
            r3 = subprocess.run(
                [sys.executable, os.path.join(tmp_tests, "run_all.py")],
                cwd=tmp, capture_output=True, text=True, timeout=30,
                env=_env_with_probe(f"127.0.0.1:{port}"))
        finally:
            listener.close()
        assert r3.returncode != 0, r3.stdout + r3.stderr
        assert "FAIL test_step92_fake_daemon.py" in r3.stdout, r3.stdout
        assert "FAIL test_step93_fake_e2e.py" in r3.stdout, r3.stdout
        assert "PASS test_step94_e2e_but_no_broker.py" in r3.stdout, r3.stdout

        # Кривой адрес -- ошибка раннера (код 2), а не тихий откат к
        # 127.0.0.1:1883.
        r4 = subprocess.run(
            [sys.executable, os.path.join(tmp_tests, "run_all.py")],
            cwd=tmp, capture_output=True, text=True, timeout=30,
            env=_env_with_probe("не-адрес"))
        assert r4.returncode == 2, r4.stdout + r4.stderr
        assert "WBEM_TEST_MQTT_PROBE" in r4.stderr, r4.stderr
        print("[OK] *_daemon.py/*_e2e.py распознаются по имени и пропускаются "
              "(флагом и при недоступном брокере), запускаются при доступном; "
              "файл с 'e2e' не как точный суффикс и с mosquitto/1883 в тексте "
              "-- запускается как обычно; кривой WBEM_TEST_MQTT_PROBE -- код 2")


def _closed_port():
    """Порт, на котором заведомо никто не слушает: занять свободный порт
    и сразу отпустить."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _env_with_probe(value):
    env = dict(os.environ)
    env["WBEM_TEST_MQTT_PROBE"] = value
    return env


def test_missing_file_protection_guard():
    """Белый ящик: имитируем баг раннера, из-за которого основной отбор
    (_discover) насчитал МЕНЬШЕ файлов, чем независимый повторный glob()
    прямо перед печатью сводки -- именно та ситуация, из-за которой
    test_step40/41 (партия 7) тихо выпали из ci.yml. Раннер обязан
    отказаться работать, а не продолжить с неполным списком."""
    mod = _load_run_all_module()
    real_glob = mod.glob.glob
    calls = {"n": 0}

    def fake_glob(pattern):
        calls["n"] += 1
        real = real_glob(pattern)
        if calls["n"] == 1:
            return real[:-1] if real else real  # первый вызов -- будто один потерялся
        return real

    mod.glob.glob = fake_glob
    try:
        rc = mod.run([])
    finally:
        mod.glob.glob = real_glob

    assert rc != 0, "раннер должен был отказаться работать при расхождении счётчиков"
    print("[OK] раннер ловит собственное расхождение в счётчике файлов "
          "(имитация регрессии test_step40/41) и отказывается продолжать")


if __name__ == "__main__":
    test_bash_python_syntax_and_help()
    test_runner_reports_same_count_as_real_glob()
    test_only_narrows_selection_and_bad_pattern_is_error()
    test_failing_file_gives_nonzero_exit_in_isolated_copy()
    test_mosquitto_detection_by_name_and_skip_flag()
    test_missing_file_protection_guard()
    print("\nВсе тесты Шага 48 (единый раннер тестов) пройдены.")
