"""kanban-tui backend for luskctl container orchestration.

Maps luskctl concepts to kanban-tui models:
- Projects  -> Boards   (one board per luskctl project)
- Statuses  -> Columns   (Created / Running / Stopped / Completed / Failed)
- Tasks     -> Cards      (with live container state from podman)

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
    _resolve_config_roots,
    _resolve_state_root,
    discover_projects,
    effective_status,
    query_container_states,
    read_task_metas,
    resolve_task_container_state,
)

# Column definitions — fixed status columns for all luskctl boards.
_COLUMNS: list[tuple[int, str]] = [
    (1, "Created"),
    (2, "Running"),
    (3, "Stopped"),
    (4, "Completed"),
    (5, "Failed"),
]

_STATUS_TO_COLUMN: dict[str, int] = {
    "created": 1,
    "running": 2,
    "stopped": 3,
    "not found": 3,  # treat as stopped
    "completed": 4,
    "failed": 5,
    "deleting": 3,  # show in stopped while deletion proceeds
}

_COLUMN_TO_ACTION: dict[int, str] = {
    2: "restart",  # drag to Running -> restart container
    3: "stop",  # drag to Stopped -> stop container
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
    "online": "🌐",
    "gatekeeping": "🔒",
}


@dataclass
class LuskctlBackend(Backend):
    """Backend reading luskctl task state from YAML files + podman.

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

    # === Board Management ===

    def get_boards(self) -> list[Board]:
        self._refresh_projects()
        boards: list[Board] = []
        for proj in self._projects:
            bid = self._project_id_to_board_id[proj.project_id]
            icon = _SECURITY_ICONS.get(proj.security_class, "📦")
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
                    reset_column=1,  # Created
                    start_column=2,  # Running
                    finish_column=4,  # Completed
                )
            )
        return boards

    @property
    def active_board(self) -> Board:
        boards = self.get_boards()
        if not boards:
            raise Exception("No luskctl projects found")
        active_pid = self._active_project_id()
        for board in boards:
            if board.name == active_pid:
                return board
        return boards[0]

    def get_board_infos(self):
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
        for col in self.get_columns():
            if col.column_id == column_id:
                return col
        return None

    # === Task Management ===

    def _luskctl_to_kanban_task(
        self,
        meta: LuskctlTaskMeta,
        project_id: str,
        container_states: dict[str, str],
    ) -> Task:
        """Convert a luskctl task to a kanban-tui Task."""
        cs = resolve_task_container_state(project_id, meta, container_states)
        status = effective_status(cs, meta.mode, meta.exit_code, meta.deleting)
        column = _STATUS_TO_COLUMN.get(status, 1)

        # Infer dates from status
        now = datetime.now()
        start_date = now if status in ("running", "completed") else None
        finish_date = now if status == "completed" else None

        # Map mode to category
        category = _MODE_TO_CATEGORY.get(meta.mode) if meta.mode else None

        # Build description from metadata
        desc_parts: list[str] = []
        if meta.mode:
            desc_parts.append(f"Mode: {meta.mode}")
        if meta.backend:
            desc_parts.append(f"Backend: {meta.backend}")
        if meta.preset:
            desc_parts.append(f"Preset: {meta.preset}")
        if meta.web_port:
            desc_parts.append(f"Port: {meta.web_port}")
        description = " | ".join(desc_parts)

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
                "source": "luskctl",
            },
        )

    def get_tasks_on_active_board(self) -> list[Task]:
        return self.get_tasks_by_board(self.active_board.board_id)

    def get_tasks_by_board(self, board_id: int) -> list[Task]:
        pid = self._board_id_to_project_id.get(board_id)
        if not pid:
            return []

        metas = read_task_metas(pid, self._state_root)
        if not metas:
            return []

        container_states = query_container_states(pid)
        return [
            self._luskctl_to_kanban_task(m, pid, container_states) for m in metas
        ]

    def get_task_by_id(self, task_id: int) -> Task | None:
        for task in self.get_tasks_on_active_board():
            if task.task_id == task_id:
                return task
        return None

    def get_tasks_by_ids(self, task_ids: list[int]) -> list[Task]:
        all_tasks = self.get_tasks_on_active_board()
        id_set = set(task_ids)
        return [t for t in all_tasks if t.task_id in id_set]

    # === Category Management ===

    def get_all_categories(self) -> list[Category]:
        return [
            Category(category_id=cid, name=name, color=color)
            for cid, name, color in _MODE_CATEGORIES
        ]

    def get_category_by_id(self, category_id: int) -> Category:
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
        pid = self._active_project_id()
        cli_bridge.task_delete(pid, str(task_id))

    def update_task_status(self, new_task: Task):
        """Handle column changes — trigger container lifecycle actions."""
        pid = new_task.metadata.get("project_id", self._active_project_id())
        tid = str(new_task.task_id)
        action = _COLUMN_TO_ACTION.get(new_task.column)
        if action == "restart":
            cli_bridge.task_restart(pid, tid)
        elif action == "stop":
            cli_bridge.task_stop(pid, tid)
        # Other column moves are status-only (no container action needed)

    def update_task_entry(
        self,
        task_id: int,
        title: str,
        description: str,
        category: int | None,
        due_date: datetime | None,
    ) -> Task | None:
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
