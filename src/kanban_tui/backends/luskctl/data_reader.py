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


STATUS_FILE_NAME = "work-status.yml"
"""Filename agents write inside their agent-config directory."""

PENDING_PHASE_FILE = "pending-phase.yml"
"""Filename for deferred phase transitions on running tasks."""


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
class LuskctlWorkStatus:
    """Parsed work status from an agent's status file."""

    status: str | None = None
    message: str | None = None


@dataclass
class LuskctlPendingPhase:
    """Deferred phase transition queued on a running task."""

    phase: str
    prompt: str


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
    work_status: str | None = None
    work_message: str | None = None
    pending_phase: LuskctlPendingPhase | None = None


@dataclass
class LuskctlProjectInfo:
    """Minimal project info discovered from the filesystem."""

    project_id: str
    root: Path
    security_class: str = "online"


# ---------- Work status I/O ----------


def read_work_status(agent_config_dir: Path) -> LuskctlWorkStatus:
    """Read ``work-status.yml`` from *agent_config_dir*.

    Returns empty ``LuskctlWorkStatus`` if the file is missing, empty,
    or malformed.  A bare string is accepted as a status-only value.
    """
    status_path = agent_config_dir / STATUS_FILE_NAME
    if not status_path.is_file():
        return LuskctlWorkStatus()
    try:
        raw = yaml.safe_load(status_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return LuskctlWorkStatus()
    if raw is None:
        return LuskctlWorkStatus()
    if isinstance(raw, str):
        return LuskctlWorkStatus(status=raw)
    if isinstance(raw, dict):
        return LuskctlWorkStatus(
            status=raw.get("status"),
            message=raw.get("message"),
        )
    return LuskctlWorkStatus()


def write_work_status(agent_config_dir: Path, status: str | None) -> bool:
    """Write ``work-status.yml`` to *agent_config_dir*.

    When *status* is ``None``, removes the file.  Returns ``True`` on success.
    """
    status_path = agent_config_dir / STATUS_FILE_NAME
    try:
        if status is None:
            if status_path.is_file():
                status_path.unlink()
            return True
        agent_config_dir.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            yaml.safe_dump({"status": status}), encoding="utf-8"
        )
        return True
    except OSError:
        return False


# ---------- Pending phase I/O ----------


def read_pending_phase(agent_config_dir: Path) -> LuskctlPendingPhase | None:
    """Read ``pending-phase.yml`` from *agent_config_dir*.

    Returns ``None`` if the file is missing, empty, or malformed.
    """
    phase_path = agent_config_dir / PENDING_PHASE_FILE
    if not phase_path.is_file():
        return None
    try:
        raw = yaml.safe_load(phase_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    phase = raw.get("phase")
    prompt = raw.get("prompt", "")
    if not phase:
        return None
    return LuskctlPendingPhase(phase=str(phase), prompt=str(prompt))


def write_pending_phase(agent_config_dir: Path, phase: str, prompt: str) -> bool:
    """Write ``pending-phase.yml`` to *agent_config_dir*.

    Returns ``True`` on success.
    """
    try:
        agent_config_dir.mkdir(parents=True, exist_ok=True)
        phase_path = agent_config_dir / PENDING_PHASE_FILE
        phase_path.write_text(
            yaml.safe_dump({"phase": phase, "prompt": prompt}), encoding="utf-8"
        )
        return True
    except OSError:
        return False


def clear_pending_phase(agent_config_dir: Path) -> None:
    """Remove ``pending-phase.yml`` from *agent_config_dir``."""
    phase_path = agent_config_dir / PENDING_PHASE_FILE
    try:
        if phase_path.is_file():
            phase_path.unlink()
    except OSError:
        pass


def _agent_config_dir(state_root: Path, project_id: str, task_id: str) -> Path:
    """Return the agent-config directory for a task."""
    return state_root / "projects" / project_id / "tasks" / task_id / "agent-config"


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
    """Read all task metadata YAML files for a project.

    Enriches each task with work status and pending phase from agent-config.
    """
    if state_root is None:
        state_root = _resolve_state_root()
    meta_dir = state_root / "projects" / project_id / "tasks"
    if not meta_dir.is_dir():
        return []

    tasks: list[LuskctlTaskMeta] = []
    for f in meta_dir.glob("*.yml"):
        try:
            meta = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            tid = str(meta.get("task_id", f.stem))

            # Read work status + pending phase from agent-config
            ac_dir = _agent_config_dir(state_root, project_id, tid)
            ws = read_work_status(ac_dir)
            pp = read_pending_phase(ac_dir)

            tasks.append(
                LuskctlTaskMeta(
                    task_id=tid,
                    name=meta.get("name", ""),
                    mode=meta.get("mode"),
                    workspace=meta.get("workspace", ""),
                    web_port=meta.get("web_port"),
                    backend=meta.get("backend"),
                    exit_code=meta.get("exit_code"),
                    deleting=bool(meta.get("deleting")),
                    preset=meta.get("preset"),
                    work_status=ws.status,
                    work_message=ws.message,
                    pending_phase=pp,
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
