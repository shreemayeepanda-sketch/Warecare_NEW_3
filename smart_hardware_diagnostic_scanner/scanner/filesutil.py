"""Largest-files finder (read-only, bounded by depth cap and time budget).

The scan never deletes, moves, or writes anything. Directories are only
listed; symlinks (and Windows junctions) are never followed, so symlink
bombs cannot loop the walk. The function is safe to call from a background
thread and never raises: failures degrade into the note string.
"""
from __future__ import annotations

import heapq
import itertools
import logging
import os
import time
from dataclasses import dataclass

log = logging.getLogger("shds.filesutil")

_CHECK_EVERY = 256  # scandir entries between time-budget checks


@dataclass
class BigFile:
    """One large regular file hit: path as given and size in bytes."""

    path: str = ""
    size_bytes: int = 0


def largest_files(
    root: str | Path,
    limit: int = 25,
    time_budget_s: float = 20.0,
    max_depth: int = 8,
) -> tuple[list[BigFile], str]:
    """Return the top-N largest regular files under root, largest first.

    Strictly read-only. Permission/OSError on entering a subdirectory skips
    just that directory (counted in the note). The time budget is checked
    every 256 entries via time.monotonic(); on exhaustion the scan stops and
    the note says it is partial. A file passed as root returns just that
    file. Never raises.
    """
    try:
        limit = max(1, int(limit))
        max_depth = max(0, int(max_depth))
        time_budget_s = float(time_budget_s)
        root_str = os.fspath(root)
    except (TypeError, ValueError) as exc:
        return [], f"Invalid largest_files parameters: {exc}"

    if not root_str:
        return [], "path does not exist (empty path given)"
    try:
        if not os.path.exists(root_str):
            return [], f"path does not exist: {root_str}"
    except OSError as exc:
        return [], f"Cannot inspect path {root_str}: {exc}"

    if os.path.isfile(root_str):
        try:
            size = os.path.getsize(root_str)
        except OSError as exc:
            return [], f"Cannot read file size of {root_str}: {exc}"
        note = f"Root is a single file: {size} byte(s)."
        return [BigFile(path=root_str, size_bytes=size)], note
    if not os.path.isdir(root_str):
        return [], f"Root is neither a directory nor a regular file: {root_str}"

    deadline = time.monotonic() + max(0.0, time_budget_s)
    heap: list[tuple[int, int, str]] = []  # min-heap of (size, seq, path)
    counter = itertools.count()
    entries_seen = 0
    files_seen = 0
    dirs_skipped = 0
    partial = False

    stack: list[tuple[str, int]] = [(root_str, 0)]
    while stack and not partial:
        current, depth = stack.pop()
        try:
            listing = os.scandir(current)
        except OSError as exc:
            if current == root_str:
                return [], f"Cannot read root directory {root_str}: {exc}"
            dirs_skipped += 1
            log.debug("skipping directory %s: %s", current, exc)
            continue
        try:
            with listing as it:
                for entry in it:
                    # Budget check every 256 entries (and once at the start).
                    if entries_seen % _CHECK_EVERY == 0 and time.monotonic() >= deadline:
                        partial = True
                        break
                    entries_seen += 1
                    try:
                        if entry.is_symlink():
                            continue
                        is_junction = getattr(entry, "is_junction", None)
                        if callable(is_junction) and is_junction():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if depth < max_depth:
                                stack.append((entry.path, depth + 1))
                            continue
                        if entry.is_file(follow_symlinks=False):
                            try:
                                size = entry.stat(follow_symlinks=False).st_size
                            except OSError:
                                continue
                            files_seen += 1
                            if len(heap) < limit:
                                heapq.heappush(heap, (size, next(counter), entry.path))
                            elif size > heap[0][0]:
                                heapq.heapreplace(heap, (size, next(counter), entry.path))
                    except OSError:
                        continue  # one unreadable entry never stops the scan
        except OSError as exc:
            if current == root_str:
                return [], f"Cannot read root directory {root_str}: {exc}"
            dirs_skipped += 1
            log.debug("error while listing %s: %s", current, exc)

    results = [BigFile(path=path_str, size_bytes=size)
               for size, _seq, path_str in heapq.nlargest(len(heap), heap)]

    summary = [f"{files_seen} file(s) seen"]
    if dirs_skipped:
        summary.append(f"{dirs_skipped} dir(s) skipped (access errors)")
    if partial:
        summary.append(f"partial scan — time budget reached after {files_seen} file(s) seen")
    note = f"Largest-file scan of {root_str}: " + "; ".join(summary) + "."
    return results, note
