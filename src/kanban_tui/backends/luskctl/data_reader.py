"""Read luskctl task state from YAML files and podman container state.

This module has zero dependency on the luskctl Python package.  It reads
YAML files from the luskctl state directory and queries podman via
subprocess to compute the same effective task status that luskctl uses.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml


# ---------- Path resolution ----------


def _resolve_state_root() -> Path:
    """Resolve luskctl state root: $LUSKCTL_STATE_DIR > XDG > default."""
    env = os.environ.get("LUSKCTL_STATE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "luskctl"


def _resolve_config_roots() -> list[Path]:
    """Resolve luskctl config directories (user + system).

    Returns a list of directories to scan for project subdirectories.
    """
    roots: list[Path] = []

    env = os.environ.get("LUSKCTL_CONFIG_DIR")
    if env:
        base = Path(env).expanduser().resolve()
        projects = base / "projects"
        roots.append(projects if projects.is_dir() else base)
        return roots

    xdg = os.environ.get("XDG_CONFIG_HOME")
    user_root = (Path(xdg) if xdg else Path.home() / ".config") / "luskctl" / "projects"
    roots.append(user_root)
    roots.append(Path("/etc/luskctl"))
    return roots


# ---------- Data model ----------


@dataclass
class LuskctlTaskMeta:
    """Lightweight mirror of luskctl's TaskMeta — only fields we need."""

    task_id: str
    name: str = ""
    mode: str | None = None
    workspace: str = ""
    web_port: int | None = None
    backend: str | None = None
    exit_code: int | None = None
    deleting: bool = False
    preset: str | None = None


@dataclass
class LuskctlProjectInfo:
    """Minimal project info discovered from the filesystem."""

    project_id: str
    root: Path
    security_class: str = "online"


# ---------- Effective status ----------


def effective_status(
    container_state: str | None,
    mode: str | None,
    exit_code: int | None,
    deleting: bool,
) -> str:
    """Compute effective task status — mirrors luskctl's task_display.effective_status.

    Returns one of: "deleting", "running", "stopped", "completed",
    "failed", "created", "not found".
    """
    if deleting:
        return "deleting"

    if container_state == "running":
        return "running"

    if container_state is not None:
        if exit_code is not None and exit_code == 0:
            return "completed"
        if exit_code is not None and exit_code != 0:
            return "failed"
        return "stopped"

    # No container found
    if mode is None:
        return "created"
    if exit_code is not None and exit_code == 0:
        return "completed"
    if exit_code is not None and exit_code != 0:
        return "failed"
    return "not found"


# ---------- Project discovery ----------


def discover_projects(
    config_roots: list[Path] | None = None,
) -> list[LuskctlProjectInfo]:
    """Discover luskctl projects from config directories.

    Each subdirectory containing a ``project.yml`` is treated as a project.
    User directories take precedence over system ones (by ID).
    """
    if config_roots is None:
        config_roots = _resolve_config_roots()

    seen: dict[str, LuskctlProjectInfo] = {}
    for root in config_roots:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            yml = d / "project.yml"
            if not yml.is_file():
                continue
            try:
                cfg = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
            except (yaml.YAMLError, OSError):
                continue
            proj_cfg = cfg.get("project", {}) or {}
            pid = proj_cfg.get("id", d.name)
            sec = proj_cfg.get("security_class", "online")
            # Later roots overwrite earlier ones (user > system)
            seen[pid] = LuskctlProjectInfo(project_id=pid, root=d, security_class=sec)

    return sorted(seen.values(), key=lambda p: p.project_id)


# ---------- Task reading ----------


def read_task_metas(
    project_id: str,
    state_root: Path | None = None,
) -> list[LuskctlTaskMeta]:
    """Read all task metadata YAML files for a project."""
    if state_root is None:
        state_root = _resolve_state_root()
    meta_dir = state_root / "projects" / project_id / "tasks"
    if not meta_dir.is_dir():
        return []

    tasks: list[LuskctlTaskMeta] = []
    for f in meta_dir.glob("*.yml"):
        try:
            meta = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            tasks.append(
                LuskctlTaskMeta(
                    task_id=str(meta.get("task_id", f.stem)),
                    name=meta.get("name", ""),
                    mode=meta.get("mode"),
                    workspace=meta.get("workspace", ""),
                    web_port=meta.get("web_port"),
                    backend=meta.get("backend"),
                    exit_code=meta.get("exit_code"),
                    deleting=bool(meta.get("deleting")),
                    preset=meta.get("preset"),
                )
            )
        except (yaml.YAMLError, OSError, KeyError):
            continue

    # Sort: numeric IDs first (ascending), then non-numeric lexically
    def _sort_key(t: LuskctlTaskMeta) -> tuple[bool, int, str]:
        try:
            return (False, int(t.task_id), t.task_id)
        except (ValueError, TypeError):
            return (True, 0, t.task_id or "")

    tasks.sort(key=_sort_key)
    return tasks


# ---------- Container state ----------


_CONTAINER_MODES = ("cli", "web", "run")


def query_container_states(project_id: str) -> dict[str, str]:
    """Query podman for all container states matching a project.

    Returns ``{container_name: state}`` where state is lowercase
    (e.g. "running", "exited").  Returns empty dict if podman is
    unavailable.
    """
    try:
        out = subprocess.check_output(
            [
                "podman",
                "ps",
                "-a",
                "--filter",
                f"name=^{project_id}-",
                "--format",
                "{{.Names}} {{.State}}",
                "--no-trunc",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return {}

    result: dict[str, str] = {}
    for line in out.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            result[parts[0]] = parts[1].lower()
    return result


def resolve_task_container_state(
    project_id: str,
    task: LuskctlTaskMeta,
    container_states: dict[str, str],
) -> str | None:
    """Look up a task's container state from a batch query result."""
    if not task.mode:
        return None
    cname = f"{project_id}-{task.mode}-{task.task_id}"
    return container_states.get(cname)
