"""Filesystem helpers (target architecture, section 3): atomic directory and file replacement, modes, removal and backup rotation."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Callable, Optional


def replace_directory(source: Path, destination: Path, prepare: Optional[Callable[[Path], None]] = None) -> None:
    """Replace ``destination`` with a copy of ``source`` using two renames.

    The copy is built next to the destination as ``<destination>.new``; ``prepare`` (permissions,
    owner, cleanup) runs on that copy before it goes live. Then ``destination`` is renamed to
    ``<destination>.old`` and ``<destination>.new`` to ``destination``. ``destination`` never holds a
    partially copied tree: it is the complete old or the complete new one, except for the instant
    between the two renames when it is absent and the complete old tree is in ``.old``. ``.old`` is
    removed after a successful swap. Any error before the first rename leaves ``destination`` untouched;
    an error on the second rename puts the old tree back. Leftovers of an interrupted run are
    handled first: if ``destination`` is missing but ``.old`` exists (the run stopped between the
    two renames), ``.old`` is the complete previous tree and is renamed back; otherwise a stale
    ``.old`` is removed. A stale ``.new`` is always removed. Errors propagate as ``OSError``.
    """
    new_dir = destination.with_name(destination.name + ".new")
    old_dir = destination.with_name(destination.name + ".old")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if _exists(old_dir) and not _exists(destination):
        os.rename(old_dir, destination)
    _remove_tree(new_dir)
    _remove_tree(old_dir)

    try:
        shutil.copytree(source, new_dir, symlinks=False)
        if prepare is not None:
            prepare(new_dir)
    except BaseException:
        _remove_tree(new_dir)
        raise

    had_destination = _exists(destination)
    if had_destination:
        os.rename(destination, old_dir)
    try:
        os.rename(new_dir, destination)
    except BaseException:
        if had_destination:
            os.rename(old_dir, destination)
        _remove_tree(new_dir)
        raise
    if had_destination:
        _remove_tree(old_dir)


def replace_file(destination: Path, data: bytes, prepare: Optional[Callable[[Path], None]] = None) -> None:
    """Replace ``destination`` with ``data`` using one rename.

    The data is written next to the destination as hidden ``.<name>.new``; ``prepare`` (mode, owner)
    runs on that copy before ``os.replace`` makes it live, so ``destination`` is always the complete
    old or the complete new file. Any error leaves ``destination`` untouched and removes the copy;
    a stale copy from an interrupted run is overwritten. Errors propagate as ``OSError``.
    """
    new_file = destination.with_name("." + destination.name + ".new")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _remove_tree(new_file)
    try:
        new_file.write_bytes(data)
        if prepare is not None:
            prepare(new_file)
        os.replace(new_file, destination)
    except BaseException:
        _remove_tree(new_file)
        raise


def set_root_executable(path: Path) -> None:
    """Mode 0755 and owner root:root (only when running as root) for a command in /usr/local/sbin."""
    os.chmod(path, 0o755)
    if os.geteuid() == 0:
        shutil.chown(path, user="root", group="root")


def remove_path(path: Path) -> None:
    """Remove a file, a symlink or a directory tree; a missing path is ignored."""
    _remove_tree(path)


def prune_backups(path: Path, *, keep: int) -> list[Path]:
    """Keep the ``keep`` newest ``<name>.bak.<digits>`` copies of ``path`` and remove the older ones.

    Only regular files in ``path.parent`` whose name is exactly ``<path.name>.bak.<digits>`` count
    as copies; copies of other files, other suffixes (``.bak.telegram.*``) and directories are left
    alone. Copies are ordered by the numeric timestamp, so the newest stay. Rotation is best effort:
    a missing directory or a copy that cannot be removed is skipped. Returns the removed copies,
    oldest first. ``keep`` must be at least 1 so the copy just made by the caller survives.
    """
    if keep < 1:
        raise ValueError("keep must be at least 1")
    pattern = re.compile(re.escape(path.name) + r"\.bak\.([0-9]+)")
    try:
        entries = list(path.parent.iterdir())
    except OSError:
        return []
    copies = []
    for entry in entries:
        match = pattern.fullmatch(entry.name)
        if match and entry.is_file() and not entry.is_symlink():
            copies.append((len(match.group(1)), match.group(1), entry))
    copies.sort()
    removed = []
    for _, _, entry in copies[:-keep]:
        try:
            entry.unlink()
        except OSError:
            continue
        removed.append(entry)
    return removed


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
