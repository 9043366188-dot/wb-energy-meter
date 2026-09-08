"""Самообновление из GitHub (ветка main), ТЗ v0.9.0.

Только стандартная библиотека — `urllib.request`, `json`, `subprocess`,
`os`, `re`. Никаких новых зависимостей.

Модуль отвечает за:
- сверку установленной версии с версией в ветке `main` на GitHub
  (`check_remote`, `is_update_available`);
- чтение/атомарную запись файла статуса обновления
  (`read_status`/`write_status`) — сам процесс обновления делает
  `scripts/self-update.sh`, но и он, и этот модуль пишут в один и тот же
  файл через `write_status`, чтобы формат и атомарность были едины;
- запуск апдейтера (`start_update`).

Про запуск апдейтера отдельным юнитом — см. AGENTS.md, раздел про
`systemd-run` и cgroup: апдейтер НЕЛЬЗЯ запускать обычным дочерним
процессом сервиса, иначе `systemctl stop` из install.sh прибьёт его
вместе с сервисом посреди обновления (KillMode=mixed убивает всю cgroup
wb-energy-meter.service, плюс MemoryMax=256M на неё же).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from . import __version__

log = logging.getLogger(__name__)


class UpdateCheckError(Exception):
    """Ошибка обращения к GitHub при проверке обновлений (сеть, таймаут,
    неожиданный ответ). Текст — по-русски, отдаётся прямо в API-ответе."""


class UpdateInProgressError(Exception):
    """Попытка запустить обновление, пока предыдущее ещё выполняется."""


# Состояния, при которых обновление считается "уже идёт" — совпадают с
# промежуточными (не терминальными) состояниями из STATUS_FILE, см.
# scripts/self-update.sh и docs/TZ-v0.9.0-selfupdate.md §4.3.
ACTIVE_STATES = frozenset({
    "starting", "downloading", "installing", "verifying", "rolling_back",
})

_VERSION_RE = re.compile(r"__version__\s*=\s*[\"']([^\"']+)[\"']")


# ---------------------------------------------------------------------
# Установленная версия
# ---------------------------------------------------------------------

def get_installed_info(install_dir: str) -> dict:
    """Читает <install_dir>/VERSION.json. Если файла нет или он битый —
    считаем, что установка была не через self-update (например, самый
    первый install.sh), и отдаём текущую версию пакета без commit."""
    path = os.path.join(install_dir, "VERSION.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("VERSION.json: корень не объект")
        return {
            "version": data.get("version") or __version__,
            "commit": data.get("commit"),
            "ref": data.get("ref"),
            "installed_at": data.get("installed_at"),
        }
    except (OSError, ValueError):
        return {"version": __version__, "commit": None,
                "ref": None, "installed_at": None}


# ---------------------------------------------------------------------
# Проверка удалённой версии на GitHub
# ---------------------------------------------------------------------

def parse_version_from_init(text: Optional[str]) -> Optional[str]:
    """Достаёт __version__ = "х.у.z" из текста __init__.py. Понимает
    одинарные и двойные кавычки и лишние пробелы вокруг '='."""
    if not text:
        return None
    m = _VERSION_RE.search(text)
    return m.group(1) if m else None


def _http_get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(
        url, headers={
            "User-Agent": f"wb-energy-meter/{__version__}",
            "Accept": "application/vnd.github+json",
        })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def check_remote(owner: str, repo: str, ref: str = "main",
                  timeout: float = 10) -> dict:
    """Запрашивает GitHub API за последним коммитом ветки/тега `ref` и
    версию из raw __init__.py на той же ветке.

    Возвращает {"version", "commit", "message", "date"}.
    Бросает UpdateCheckError (текст по-русски) при любой сетевой ошибке,
    таймауте или неожиданном ответе."""
    api_url = ("https://api.github.com/repos/%s/%s/commits/%s" %
               (urllib.parse.quote(owner), urllib.parse.quote(repo),
                urllib.parse.quote(ref)))
    try:
        raw = _http_get(api_url, timeout)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        raise UpdateCheckError(
            f"GitHub API вернул ошибку {e.code} для {owner}/{repo}@{ref}"
            + (f": {detail}" if detail else "")) from e
    except urllib.error.URLError as e:
        raise UpdateCheckError(
            f"Не удалось подключиться к GitHub: {e.reason}") from e
    except (TimeoutError, OSError) as e:
        raise UpdateCheckError(
            f"Превышено время ожидания ответа GitHub: {e}") from e

    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise UpdateCheckError(
            f"Некорректный ответ GitHub API: {e}") from e

    sha = data.get("sha") if isinstance(data, dict) else None
    if not sha:
        raise UpdateCheckError(
            "GitHub API не вернул sha коммита — проверьте REPO_OWNER/"
            "REPO_NAME/ref в конфиге")
    commit_info = (data.get("commit") or {}) if isinstance(data, dict) else {}
    message = str(commit_info.get("message") or "").split("\n", 1)[0]
    date = (commit_info.get("author") or {}).get("date")

    raw_url = ("https://raw.githubusercontent.com/%s/%s/%s/"
                "wb_energy_meter/__init__.py" %
                (urllib.parse.quote(owner), urllib.parse.quote(repo),
                 urllib.parse.quote(ref)))
    try:
        init_bytes = _http_get(raw_url, timeout)
        init_text = init_bytes.decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UpdateCheckError(
            f"Не удалось получить версию из ветки {ref}: {e}") from e

    version = parse_version_from_init(init_text)
    return {"version": version, "commit": sha, "message": message,
            "date": date}


def is_update_available(installed: dict, remote: dict) -> bool:
    """True, если installed['commit'] пуст либо отличается от
    remote['commit']. Версии намеренно не сравниваются — источник это
    ветка, а не теги, поэтому единственный надёжный признак новизны —
    sha коммита."""
    installed_commit = (installed or {}).get("commit")
    remote_commit = (remote or {}).get("commit")
    if not installed_commit:
        return True
    return installed_commit != remote_commit


# ---------------------------------------------------------------------
# Файл статуса — атомарное чтение/запись, слияние полей
# ---------------------------------------------------------------------

def read_status(path: str) -> dict:
    """Никогда не бросает исключение: нет файла или битый JSON -> idle."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "state" not in data:
            return {"state": "idle"}
        return data
    except (OSError, ValueError):
        return {"state": "idle"}


def write_status(path: str, **fields: Any) -> dict:
    """Атомарная запись: во временный файл рядом, затем os.replace —
    иначе конкурентный GET /api/update/status мог бы прочитать
    обрезанный файл на медленной SD-карте контроллера.

    Сливает переданные поля с уже существующими в файле (а не
    перезаписывает документ целиком), чтобы каждый шаг self-update.sh
    мог обновлять только state/step_label, не теряя from_version,
    started_at и т. п., записанные на более раннем шаге."""
    current = read_status(path)
    if current.get("state") == "idle" and not os.path.exists(path):
        current = {}
    current.update(fields)

    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp_path, path)
    return current


# ---------------------------------------------------------------------
# Запуск апдейтера
# ---------------------------------------------------------------------

def _default_process_launcher(cmd, env=None):
    """Настоящий запуск процесса. Подменяется в тестах — start_update()
    не должен реально ничего запускать при прогоне тестов."""
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update(env)
    subprocess.Popen(
        cmd, env=full_env, cwd="/",
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def _self_update_script_path(install_dir: str) -> str:
    return os.path.join(install_dir, "scripts", "self-update.sh")


def start_update(*, install_dir: str, status_path: str,
                  repo_owner: str, repo_name: str, ref: str,
                  expected_sha: str, http_port: int,
                  process_launcher: Optional[Callable] = None) -> dict:
    """Проверяет, что обновление не идёт, пишет статус 'starting' и
    запускает scripts/self-update.sh отдельным transient-юнитом через
    `systemd-run --collect` (см. модульный докстринг: НЕ subprocess из
    самого сервиса — cgroup/KillMode/MemoryMax).

    Возвращает управление немедленно, ничего не ждёт. `process_launcher`
    — точка подмены в тестах: `(cmd: list[str], env: dict|None) -> None`.
    """
    current = read_status(status_path)
    if current.get("state") in ACTIVE_STATES:
        raise UpdateInProgressError(current.get("state"))

    script_path = _self_update_script_path(install_dir)
    prev = get_installed_info(install_dir)

    env_vars = {
        "REPO_OWNER": str(repo_owner),
        "REPO_NAME": str(repo_name),
        "REF": str(ref),
        "EXPECTED_SHA": str(expected_sha),
        "STATUS_FILE": str(status_path),
        "INSTALL_DIR": str(install_dir),
        "HTTP_PORT": str(http_port),
    }

    write_status(
        status_path,
        state="starting", step_label="Запуск апдейтера…",
        started_at=time.time(), finished_at=None,
        from_version=prev.get("version"), from_commit=prev.get("commit"),
        to_version=None, to_commit=expected_sha,
        error=None, log_tail=[],
    )

    cmd = [
        "systemd-run", "--unit=wb-energy-meter-update", "--collect",
        "--description=Обновление wb-energy-meter",
    ]
    for k, v in env_vars.items():
        cmd.append(f"--setenv={k}={v}")
    cmd += ["/bin/bash", script_path]

    launch = process_launcher or _default_process_launcher
    try:
        launch(cmd, None)
        log.info("Апдейтер запущен через systemd-run: %s", script_path)
    except (OSError, FileNotFoundError) as e:
        log.warning(
            "systemd-run недоступен (%s) — использую фолбэк setsid/nohup. "
            "Это НЕ рекомендуемый путь, см. AGENTS.md.", e)
        fallback_cmd = ["setsid", "nohup", "bash", script_path]
        try:
            launch(fallback_cmd, env_vars)
            log.info("Апдейтер запущен фолбэком (setsid nohup): %s",
                      script_path)
        except Exception as e2:
            write_status(
                status_path, state="failed",
                step_label="Не удалось запустить апдейтер",
                error=f"systemd-run и фолбэк оба недоступны: {e2}",
                finished_at=time.time())
            raise

    return read_status(status_path)
