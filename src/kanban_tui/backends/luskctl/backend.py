"""kanban-tui backend for luskctl container orchestration.

Maps luskctl concepts to kanban-tui models with development workflow columns:
- Projects  -> Boards   (one board per luskctl project)
- Dev phases -> Columns  (Ready / Coding / Testing / Review / Done / Stopped)
- Tasks     -> Cards     (with live container state + agent work status)

Read operations query YAML files + podman directly.
Write operations delegate to the ``luskctl`` CLI via subprocess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from kanban_tui.backends.base import Backend
from kanban_tui.classes.board import Board
from kanban_tui.classes.category import Category
from kanban_tui.classes.column import Column
from kanban_tui.classes.task import Task
from kanban_tui.config import LuskctlBackendSettings

from . import cli_bridge
from .data_reader import (
    LuskctlProjectInfo,
    LuskctlTaskMeta,
    _agent_config_dir,
    _resolve_config_roots,
    _resolve_state_root,
    clear_pending_phase,
    discover_projects,
    effective_status,
    query_container_states,
    read_task_metas,
    resolve_task_container_state,
    write_pending_phase,
    write_work_status,
)

# ---------- Column definitions (6-column development workflow) ----------

_COLUMNS: list[tuple[int, str]] = [
    (1, "Ready"),
    (2, "Coding"),
    (3, "Testing"),
    (4, "Review"),
    (5, "Done"),
    (6, "Stopped"),
]

_PHASE_PROMPTS: dict[int, str] = {
    2: "Continue implementing. Write clean, well-tested code.",
    3: "Run the test suite, analyze failures, and fix issues.",
    4: "Review all uncommitted changes. Check for bugs, style, and missing tests.",
}

_PHASE_WORK_STATUS: dict[int, str] = {
    2: "coding",
    3: "testing",
    4: "reviewing",
    5: "done",
}

_WORK_STATUS_EMOJI: dict[str, str] = {
    "planning": "\U0001f4cb",
    "coding": "\U0001f4bb",
    "testing": "\U0001f9ea",
    "debugging": "\U0001f41b",
    "reviewing": "\U0001f50d",
    "documenting": "\U0001f4dd",
    "done": "\u2705",
    "blocked": "\U0001f6a7",
    "error": "\u26a0\ufe0f",
}

# Mode categories
_MODE_CATEGORIES: list[tuple[int, str, str]] = [
    (1, "CLI", "#004578"),
    (2, "Web", "#007849"),
    (3, "Autopilot", "#b85c00"),
]

_MODE_TO_CATEGORY: dict[str, int] = {
    "cli": 1,
    "web": 2,
    "run": 3,
}

_SECURITY_ICONS: dict[str, str] = {
    "online": "\U0001f310",
    "gatekeeping": "\U0001f512",
}


def _resolve_column(
    container_status: str, work_status: str | None, mode: str | None
) -> int:
    """Map (container_status, work_status) to a column ID.

    Infrastructure states (stopped, failed, created, deleting) always
    override work status.  When the container is running, work status
    picks the column.  Running with no status file defaults to Coding.
    """
    # Infrastructure states -> fixed columns
    if container_status in ("created",):
        return 1  # Ready
    if container_status in ("stopped", "not found", "deleting", "failed"):
        return 6  # Stopped
    if container_status == "completed":
        return 5  # Done

    # Running -> use work status
    if container_status == "running":
        match work_status:
            case "testing":
                return 3
            case "reviewing" | "documenting":
                return 4
            case "done":
                return 5
            case "blocked" | "error":
                return 6
            case _:
                return 2  # Default: Coding

    return 1  # Fallback


@dataclass
class LuskctlBackend(Backend):
    """Backend mapping luskctl tasks to development workflow columns.

    Zero Python dependency on luskctl — reads filesystem state directly
    and shells out to ``luskctl`` CLI for write operations.
    """

    settings: LuskctlBackendSettings

    # Internal caches — rebuilt on each get_boards() / get_tasks*() call.
    _projects: list[LuskctlProjectInfo] = field(default_factory=list, repr=False)
    _project_id_to_board_id: dict[str, int] = field(default_factory=dict, repr=False)
    _board_id_to_project_id: dict[int, str] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self._state_root = (
            Path(self.settings.state_root).expanduser().resolve()
            if self.settings.state_root
            else _resolve_state_root()
        )
        self._config_roots = (
            [Path(self.settings.config_root).expanduser().resolve()]
            if self.settings.config_root
            else _resolve_config_roots()
        )
        self._refresh_projects()

    def _refresh_projects(self):
        """Discover projects and rebuild ID mappings."""
        self._projects = discover_projects(self._config_roots)
        self._project_id_to_board_id = {}
        self._board_id_to_project_id = {}
        for idx, p in enumerate(self._projects, start=1):
            self._project_id_to_board_id[p.project_id] = idx
            self._board_id_to_project_id[idx] = p.project_id

    def _project_for_board(self, board_id: int) -> LuskctlProjectInfo | None:
        """Return the project info for a board, or None."""
        pid = self._board_id_to_project_id.get(board_id)
        if pid is None:
            return None
        for p in self._projects:
            if p.project_id == pid:
                return p
        return None

    def _active_project_id(self) -> str:
        """Return the active project ID, falling back to first project."""
        if self.settings.active_project_id:
            return self.settings.active_project_id
        if self._projects:
            return self._projects[0].project_id
        return ""

    def _get_agent_config_dir(self, project_id: str, task_id: str) -> Path:
        """Return the agent-config directory for a task."""
        return _agent_config_dir(self._state_root, project_id, task_id)

    # === Board Management ===

    def get_boards(self) -> list[Board]:
        """Return one board per luskctl project."""
        self._refresh_projects()
        boards: list[Board] = []
        for proj in self._projects:
            bid = self._project_id_to_board_id[proj.project_id]
            icon = _SECURITY_ICONS.get(proj.security_class, "\U0001f4e6")
            try:
                ctime = proj.root.stat().st_ctime
                creation = datetime.fromtimestamp(ctime)
            except OSError:
                creation = datetime.now()
            boards.append(
                Board(
                    board_id=bid,
                    name=proj.project_id,
                    icon=icon,
                    creation_date=creation,
                    reset_column=1,   # Ready
                    start_column=2,   # Coding
                    finish_column=5,  # Done
                )
            )
        return boards

    @property
    def active_board(self) -> Board:
        """Return the active board (project)."""
        boards = self.get_boards()
        if not boards:
            raise Exception("No luskctl projects found")
        active_pid = self._active_project_id()
        for board in boards:
            if board.name == active_pid:
                return board
        return boards[0]

    def get_board_infos(self):
        """Return summary info for all boards."""
        boards = self.get_boards()
        infos = []
        for board in boards:
            tasks = self.get_tasks_by_board(board.board_id)
            infos.append(
                {
                    "board_id": board.board_id,
                    "name": board.name,
                    "icon": board.icon,
                    "amount_tasks": len(tasks),
                    "amount_columns": len(_COLUMNS),
                    "next_due": None,
                }
            )
        return infos

    # === Column Management ===

    def get_columns(self, board_id: int | None = None) -> list[Column]:
        """Return the 6 development workflow columns."""
        if board_id is None:
            board_id = self.active_board.board_id
        return [
            Column(
                column_id=cid,
                name=name,
                visible=True,
                position=idx,
                board_id=board_id,
            )
            for idx, (cid, name) in enumerate(_COLUMNS)
        ]

    def get_column_by_id(self, column_id: int) -> Column | None:
        """Find a column by its ID."""
        for col in self.get_columns():
            if col.column_id == column_id:
                return col
        return None

    # === Task Management ===

    def _build_card_description(
        self,
        meta: LuskctlTaskMeta,
        container_status: str,
    ) -> str:
        """Build rich description with emoji indicators for a task card."""
        parts: list[str] = []

        # Work status with emoji
        ws_emoji = _WORK_STATUS_EMOJI.get(meta.work_status or "", "")
        if container_status == "running":
            cs_emoji = "\U0001f7e2"
        elif container_status == "failed":
            cs_emoji = "\U0001f534"
        else:
            cs_emoji = "\U0001f7e1"

        if meta.work_status:
            parts.append(
                f"{ws_emoji} **{meta.work_status.title()}** | {cs_emoji} {container_status}"
            )
        else:
            parts.append(f"{cs_emoji} {container_status.title()}")

        # Mode/backend/preset metadata
        meta_parts: list[str] = []
        if meta.mode:
            meta_parts.append(f"Mode: {meta.mode}")
        if meta.backend:
            meta_parts.append(f"Backend: {meta.backend}")
        if meta.preset:
            meta_parts.append(f"Preset: {meta.preset}")
        if meta_parts:
            parts.append(" | ".join(meta_parts))

        # Stopped column indicators
        if container_status == "failed" and meta.exit_code is not None:
            parts.append(f"\u274c Failed (exit code {meta.exit_code})")
        elif meta.work_status == "blocked":
            parts.append("\U0001f6a7 Agent reports: blocked")
            if meta.work_message:
                parts.append(f"  {meta.work_message}")
        elif meta.work_status == "error":
            parts.append("\u26a0\ufe0f Agent reports: error")
            if meta.work_message:
                parts.append(f"  {meta.work_message}")

        # Pending phase indicator
        if meta.pending_phase:
            parts.append(f"\u23f3 Next phase: {meta.pending_phase.phase.title()}")

        return "\n".join(parts)

    def _luskctl_to_kanban_task(
        self,
        meta: LuskctlTaskMeta,
        project_id: str,
        container_states: dict[str, str],
    ) -> Task:
        """Convert a luskctl task to a kanban-tui Task."""
        cs = resolve_task_container_state(project_id, meta, container_states)
        status = effective_status(cs, meta.mode, meta.exit_code, meta.deleting)
        column = _resolve_column(status, meta.work_status, meta.mode)

        # Infer dates from status
        now = datetime.now()
        start_date = now if status in ("running", "completed") else None
        finish_date = now if status == "completed" else None

        # Map mode to category
        category = _MODE_TO_CATEGORY.get(meta.mode) if meta.mode else None

        # Build description
        description = self._build_card_description(meta, status)

        return Task(
            task_id=int(meta.task_id),
            title=meta.name or f"task-{meta.task_id}",
            column=column,
            creation_date=now,
            start_date=start_date,
            finish_date=finish_date,
            category=category,
            description=description,
            metadata={
                "project_id": project_id,
                "mode": meta.mode,
                "backend": meta.backend,
                "preset": meta.preset,
                "web_port": meta.web_port,
                "container_status": status,
                "work_status": meta.work_status,
                "source": "luskctl",
            },
        )

    def get_tasks_on_active_board(self) -> list[Task]:
        """Return tasks for the active project."""
        return self.get_tasks_by_board(self.active_board.board_id)

    def get_tasks_by_board(self, board_id: int) -> list[Task]:
        """Return tasks for a board, auto-executing pending phase transitions."""
        pid = self._board_id_to_project_id.get(board_id)
        if not pid:
            return []

        metas = read_task_metas(pid, self._state_root)
        if not metas:
            return []

        container_states = query_container_states(pid)

        # Auto-execute pending phase transitions on stopped tasks
        for meta in metas:
            if not meta.pending_phase:
                continue
            cs = resolve_task_container_state(pid, meta, container_states)
            status = effective_status(cs, meta.mode, meta.exit_code, meta.deleting)
            if status not in ("running",):
                # Container is stopped — execute the pending phase
                ac_dir = self._get_agent_config_dir(pid, meta.task_id)
                if meta.pending_phase.phase == "done":
                    write_work_status(ac_dir, "done")
                elif meta.pending_phase.prompt and meta.mode == "run":
                    write_work_status(ac_dir, meta.pending_phase.phase)
                    cli_bridge.task_followup(
                        pid, meta.task_id, meta.pending_phase.prompt
                    )
                clear_pending_phase(ac_dir)
                # Update meta fields so the card reflects the new state
                meta.work_status = meta.pending_phase.phase
                meta.pending_phase = None

        return [
            self._luskctl_to_kanban_task(m, pid, container_states) for m in metas
        ]

    def get_task_by_id(self, task_id: int) -> Task | None:
        """Find a single task by ID."""
        for task in self.get_tasks_on_active_board():
            if task.task_id == task_id:
                return task
        return None

    def get_tasks_by_ids(self, task_ids: list[int]) -> list[Task]:
        """Find multiple tasks by IDs."""
        all_tasks = self.get_tasks_on_active_board()
        id_set = set(task_ids)
        return [t for t in all_tasks if t.task_id in id_set]

    # === Category Management ===

    def get_all_categories(self) -> list[Category]:
        """Return mode-based categories."""
        return [
            Category(category_id=cid, name=name, color=color)
            for cid, name, color in _MODE_CATEGORIES
        ]

    def get_category_by_id(self, category_id: int) -> Category:
        """Find a category by ID."""
        for cat in self.get_all_categories():
            if cat.category_id == category_id:
                return cat
        raise NotImplementedError(f"Category {category_id} not found")

    # === Write Operations (via luskctl CLI) ===

    def create_new_task(
        self,
        title: str,
        description: str,
        column: int,
        category: int | None = None,
        due_date: datetime | None = None,
    ) -> Task:
        """Create a new task via luskctl CLI."""
        pid = self._active_project_id()
        task_id_str = cli_bridge.task_new(pid, name=title)
        if task_id_str is None:
            raise RuntimeError(
                "Failed to create task via luskctl CLI. "
                "Is luskctl installed and on PATH?"
            )
        # Re-read the task from disk
        task = self.get_task_by_id(int(task_id_str))
        if task is None:
            raise RuntimeError(f"Task {task_id_str} created but not found in state")
        return task

    def delete_task(self, task_id: int):
        """Delete a task via luskctl CLI."""
        pid = self._active_project_id()
        cli_bridge.task_delete(pid, str(task_id))

    def update_task_status(self, new_task: Task):
        """Handle column changes — trigger development phase transitions.

        Autopilot (mode=run) tasks support phase transitions:
        - Running -> queue pending phase (deferred until container stops)
        - Stopped -> immediate followup with phase prompt

        Interactive (mode=cli/web) tasks:
        - Running -> all phase moves blocked (agent controls phase)
        - Stopped -> restart to Coding only

        Moves to Ready (col 1) and Stopped (col 6) are always blocked.
        """
        pid = new_task.metadata.get("project_id", self._active_project_id())
        tid = str(new_task.task_id)
        target_col = new_task.column
        mode = new_task.metadata.get("mode")
        container_status = new_task.metadata.get("container_status", "")

        # Block invalid moves
        if target_col in (1, 6):  # Ready, Stopped
            return  # No action

        # Block moves for unstarted tasks
        if mode is None:
            return

        agent_config = self._get_agent_config_dir(pid, tid)

        if mode in ("cli", "web"):
            # Interactive tasks
            if container_status == "running":
                return  # Agent controls phase — blocked
            # Stopped interactive: only restart to Coding
            if target_col == 2:
                cli_bridge.task_restart(pid, tid)
            return

        # Autopilot (mode=run) tasks
        if container_status == "running":
            # Deferred: queue pending phase
            phase = _PHASE_WORK_STATUS.get(target_col, "coding")
            if target_col == 5:  # Done
                write_pending_phase(agent_config, "done", "")
            else:
                prompt = _PHASE_PROMPTS.get(target_col, "")
                if prompt:
                    write_pending_phase(agent_config, phase, prompt)
        else:
            # Stopped: execute immediately
            if target_col == 5:  # Done
                write_work_status(agent_config, "done")
            elif target_col in _PHASE_PROMPTS:
                prompt = _PHASE_PROMPTS[target_col]
                write_work_status(agent_config, _PHASE_WORK_STATUS[target_col])
                cli_bridge.task_followup(pid, tid, prompt)

    def update_task_entry(
        self,
        task_id: int,
        title: str,
        description: str,
        category: int | None,
        due_date: datetime | None,
    ) -> Task | None:
        """Rename a task via luskctl CLI."""
        pid = self._active_project_id()
        cli_bridge.task_rename(pid, str(task_id), title)
        return self.get_task_by_id(task_id)

    # === Not Implemented (projects/columns managed outside kanban-tui) ===

    def create_new_board(
        self,
        name: str,
        icon: str | None = None,
        column_dict: dict[str, bool] | None = None,
    ) -> Board:
        raise NotImplementedError(
            "luskctl projects are managed outside kanban-tui. "
            "Use 'luskctl project-init' to create projects."
        )

    def delete_board(self, board_id: int):
        raise NotImplementedError(
            "luskctl projects cannot be deleted from kanban-tui."
        )

    def update_board(self, board_id: int, name: str, icon: str):
        raise NotImplementedError(
            "luskctl projects cannot be renamed from kanban-tui."
        )

    def create_new_category(self, name: str, color: str) -> Category:
        raise NotImplementedError("luskctl categories are derived from task modes.")

    def update_category(self, category_id: int, name: str, color: str) -> Category:
        raise NotImplementedError("luskctl categories are derived from task modes.")

    def delete_category(self, category_id: int):
        raise NotImplementedError("luskctl categories are derived from task modes.")

    def update_column_visibility(self, column_id: int, visible: bool):
        raise NotImplementedError("luskctl status columns are fixed.")

    def update_column_name(self, column_id: int, new_name: str):
        raise NotImplementedError("luskctl status columns are fixed.")

    def create_task_dependency(self, task_id: int, depends_on_task_id: int) -> int:
        raise NotImplementedError("luskctl does not support task dependencies.")

    def delete_task_dependency(self, task_id: int, depends_on_task_id: int) -> int:
        raise NotImplementedError("luskctl does not support task dependencies.")

    def would_create_dependency_cycle(
        self, task_id: int, depends_on_task_id: int
    ) -> bool:
        return False
