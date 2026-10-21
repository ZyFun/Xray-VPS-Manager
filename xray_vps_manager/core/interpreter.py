"""Python interpreter of the manager wrappers (migration plan, step 0a).

Wrappers in the bundle start with ``#!/usr/bin/env python3``. install.sh and
xray-manager-update replace that line with the absolute path of a Python >= 3.12
interpreter and record it in server.env as MANAGER_PYTHON. Selection order (user
decision 2026-10-21): MANAGER_PYTHON, python3.13, python3.12 from PATH, then
python3 itself when it is >= 3.12 — by its real versioned path, not the system
``python3`` link. A candidate must also import REQUIRED_MODULES: a Python built
without dev packages (step 0a, "Risks and checks") is skipped. The system
python3 is never replaced or shadowed.
"""

from __future__ import annotations

import os
import shutil
import string
import subprocess
from pathlib import Path
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from xray_vps_manager.core.process import run

MANAGER_PYTHON_KEY = "MANAGER_PYTHON"
MIN_PYTHON = (3, 12)
VERSIONED_CANDIDATES = ("python3.13", "python3.12")
# sqlite3 — manager.db, ssl — GitHub, zlib — tar.gz backups and releases, zoneinfo — MANAGER_TIMEZONE;
# lzma completes the module check of the migration plan (step 0a).
REQUIRED_MODULES = ("sqlite3", "ssl", "zlib", "lzma", "zoneinfo")
ENV_SHEBANG = "#!/usr/bin/env python3"
# The kernel reads the `#!` line into BINPRM_BUF_SIZE bytes (128 before Linux 5.1, then 256); since Linux 5.0
# a truncated interpreter path fails with "Exec format error" (81-R05). 127 fits any kernel.
MAX_SHEBANG_BYTES = 127
# Prints "X.Y.Z" and the REQUIRED_MODULES that do not import, separated by spaces
# (the same probe as manager_python_probe in bootstrap.sh and install.sh).
PROBE_SCRIPT = """\
import importlib, sys
missing = []
for name in {modules!r}:
    try:
        importlib.import_module(name)
    except Exception:
        missing.append(name)
print("%d.%d.%d" % sys.version_info[:3], *missing)
""".format(modules=REQUIRED_MODULES)
PROBE_TIMEOUT = 10

Version = Tuple[int, int, int]


class Interpreter(NamedTuple):
    path: str
    version: Version


class Probe(NamedTuple):
    version: Version
    missing: Tuple[str, ...]


class Selection(NamedTuple):
    interpreter: Optional[Interpreter]
    # (path, reason) of the candidates checked before the choice, in selection order.
    skipped: Tuple[Tuple[str, str], ...]


def format_version(version: Sequence[int]) -> str:
    return ".".join(str(part) for part in version)


def probe(command: Sequence[str], timeout: float = PROBE_TIMEOUT) -> Optional[Probe]:
    """Version and missing REQUIRED_MODULES of the Python started by ``command`` (a path or a shebang argv).

    None if it does not run or does not print a version.
    """
    if not command:
        return None
    try:
        # -I: модули не подхватываются из текущего каталога и PYTHONPATH (команда идёт от root).
        result = run([*command, "-I", "-c", PROBE_SCRIPT], timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    fields = (result.stdout or "").split()
    parts = fields[0].split(".") if fields else []
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return Probe((int(parts[0]), int(parts[1]), int(parts[2])), tuple(fields[1:]))


def probe_version(command: Sequence[str], timeout: float = PROBE_TIMEOUT) -> Optional[Version]:
    result = probe(command, timeout=timeout)
    return result.version if result is not None else None


def check_interpreter(path: str) -> Tuple[Optional[Interpreter], str]:
    """(Interpreter, "") for an absolute executable Python >= 3.12 with REQUIRED_MODULES usable in a shebang,
    otherwise (None, reason)."""
    if not path or not os.path.isabs(path):
        return None, "не абсолютный путь"
    # Ядро делит строку shebang по пробельным символам ASCII — те же, что [[:space:]] в bootstrap.sh/install.sh.
    if any(char in string.whitespace for char in path):
        return None, "пробел в пути не поддерживается в shebang"
    # read_server_env срезает кавычки по краям значения: такой MANAGER_PYTHON не прочитался бы обратно (81-R07).
    if "'" in path or '"' in path:
        return None, "кавычки в пути не сохраняются в server.env"
    if len(shebang_line(path).encode("utf-8")) > MAX_SHEBANG_BYTES:
        return None, f"строка shebang длиннее {MAX_SHEBANG_BYTES} байт"
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        return None, "файл не найден или не исполняемый"
    result = probe([path])
    if result is None:
        return None, "не запускается как Python"
    version_text = format_version(result.version)
    if result.version[:2] < MIN_PYTHON:
        return None, f"Python {version_text} старше 3.12"
    if result.missing:
        return None, f"у Python {version_text} нет модулей {', '.join(result.missing)}"
    return Interpreter(path, result.version), ""


def supported_interpreter(path: str) -> Optional[Interpreter]:
    return check_interpreter(path)[0]


def candidate_paths(configured: str = "", *, which: Callable[[str], Optional[str]] = shutil.which) -> List[str]:
    """Candidates in selection order without repeats: ``configured``, python3.13, python3.12, real path of python3."""
    candidates = [configured] if configured else []
    for name in VERSIONED_CANDIDATES:
        found = which(name)
        if found:
            candidates.append(found)
    found = which("python3")
    if found and os.path.isabs(found):
        candidates.append(os.path.realpath(found))
    unique: List[str] = []
    for path in candidates:
        if path not in unique:
            unique.append(path)
    return unique


def select_interpreter(configured: str = "", *, which: Callable[[str], Optional[str]] = shutil.which) -> Selection:
    """First supported candidate of ``candidate_paths``; ``skipped`` explains the rejected ones."""
    skipped = []
    for path in candidate_paths(configured, which=which):
        interpreter, reason = check_interpreter(path)
        if interpreter is not None:
            return Selection(interpreter, tuple(skipped))
        skipped.append((path, reason))
    return Selection(None, tuple(skipped))


def shebang_line(interpreter_path: str) -> str:
    return f"#!{interpreter_path}" if interpreter_path else ENV_SHEBANG


def shebang_command(line: str) -> list[str]:
    """argv of a ``#!`` line (``#!/usr/bin/env python3`` → ``['/usr/bin/env', 'python3']``); [] for other lines."""
    if not line.startswith("#!"):
        return []
    return line[2:].split()


def absolute_interpreter(command: Sequence[str]) -> str:
    """Interpreter path of a shebang argv; '' when it goes through ``env`` or is not absolute."""
    if not command or not os.path.isabs(command[0]) or os.path.basename(command[0]) == "env":
        return ""
    return command[0]


def read_shebang(path: Path) -> str:
    """First line of ``path`` without the line break; '' when the file cannot be read."""
    try:
        with path.open("rb") as handle:
            first = handle.readline(4096)
    except OSError:
        return ""
    return first.decode("utf-8", "replace").rstrip("\r\n")


# Problems of installed wrappers found by check_wrappers (xray-test "Helper interpreter").
WRAPPERS_NOT_INSTALLED = "not-installed"
WRAPPERS_MIXED = "mixed"
WRAPPERS_NO_SHEBANG = "no-shebang"
WRAPPERS_ENV = "env"
WRAPPERS_NOT_RUNNING = "not-running"
WRAPPERS_OLD = "old"
WRAPPERS_MISSING_MODULES = "missing-modules"


class WrappersCheck(NamedTuple):
    # "" when every wrapper starts with the same absolute path to Python >= 3.12 with REQUIRED_MODULES.
    problem: str
    # First line → names of the wrappers that start with it, in path order.
    shebangs: Dict[str, Tuple[str, ...]]
    line: str = ""
    interpreter: str = ""
    probe: Optional[Probe] = None


def wrapper_shebangs(paths: Iterable[Path]) -> Dict[str, Tuple[str, ...]]:
    """First line of each existing wrapper → names of the wrappers that start with it."""
    groups: Dict[str, List[str]] = {}
    for path in paths:
        if path.exists():
            groups.setdefault(read_shebang(path), []).append(path.name)
    return {line: tuple(names) for line, names in groups.items()}


def wrappers_command(paths: Iterable[Path]) -> List[str]:
    """argv of the interpreter from the first existing wrapper with a shebang; ``['python3']`` when there is none."""
    for path in paths:
        command = shebang_command(read_shebang(path))
        if command:
            return command
    return ["python3"]


def check_wrappers(paths: Iterable[Path]) -> WrappersCheck:
    """Whether the wrappers share one absolute shebang to a usable Python; ``problem`` names the first failed rule."""
    shebangs = wrapper_shebangs(paths)
    if not shebangs:
        return WrappersCheck(WRAPPERS_NOT_INSTALLED, shebangs)
    if len(shebangs) > 1:
        return WrappersCheck(WRAPPERS_MIXED, shebangs)
    line = next(iter(shebangs))
    command = shebang_command(line)
    if not command:
        return WrappersCheck(WRAPPERS_NO_SHEBANG, shebangs, line)
    result = probe(command)
    interpreter = absolute_interpreter(command)
    if not interpreter:
        return WrappersCheck(WRAPPERS_ENV, shebangs, line, "", result)
    if result is None:
        return WrappersCheck(WRAPPERS_NOT_RUNNING, shebangs, line, interpreter)
    if result.version[:2] < MIN_PYTHON:
        return WrappersCheck(WRAPPERS_OLD, shebangs, line, interpreter, result)
    if result.missing:
        return WrappersCheck(WRAPPERS_MISSING_MODULES, shebangs, line, interpreter, result)
    return WrappersCheck("", shebangs, line, interpreter, result)


def with_shebang(content: bytes, shebang: str) -> bytes:
    """``content`` of a manager wrapper with its first ``#!`` line replaced by ``shebang`` (prepended when absent).

    The wrappers are always Python, so the old line is replaced whatever interpreter it names: a backup
    may hold ``#!/opt/runtime/bin/py`` of a removed MANAGER_PYTHON (81-R04).
    """
    line = shebang.encode("utf-8") + b"\n"
    if not content.startswith(b"#!"):
        return line + content
    return line + content.partition(b"\n")[2]
