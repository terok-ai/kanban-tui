"""Subprocess wrapper for luskctl CLI write operations.

All write operations shell out to the ``luskctl`` CLI binary rather than
importing luskctl as a Python dependency.  This keeps the kanban-tui
package independent.
"""

from __future__ import annotations

import shutil
import subprocess


def luskctl_available() -> bool:
    """Return True if the ``luskctl`` binary is on PATH."""
    return shutil.which("luskctl") is not None


def _run_luskctl(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    """Run a luskctl command, returning the CompletedProcess."""
    return subprocess.run(
        ["luskctl", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def task_new(project_id: str, name: str | None = None) -> str | None:
    """Create a new luskctl task, return the task ID or None on failure.

    Parses the task ID from luskctl's stdout which prints:
    ``Created task <id> (<name>) in <path>``
    """
    args = ["task", "new", project_id]
    if name:
        args.extend(["--name", name])
    try:
        result = _run_luskctl(*args)
        if result.returncode != 0:
            return None
        # Parse "Created task 3 (name) in /path"
        for word in result.stdout.split():
            if word.isdigit():
                return word
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def task_stop(project_id: str, task_id: str) -> bool:
    """Stop a running task container. Returns True on success."""
    try:
        result = _run_luskctl("task", "stop", project_id, task_id)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def task_restart(project_id: str, task_id: str) -> bool:
    """Restart a stopped task container. Returns True on success."""
    try:
        result = _run_luskctl("task", "restart", project_id, task_id, timeout=60)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def task_delete(project_id: str, task_id: str) -> bool:
    """Delete a task and its workspace. Returns True on success."""
    try:
        result = _run_luskctl("task", "delete", project_id, task_id)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def task_rename(project_id: str, task_id: str, new_name: str) -> bool:
    """Rename a task. Returns True on success."""
    try:
        result = _run_luskctl("task", "rename", project_id, task_id, new_name)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
