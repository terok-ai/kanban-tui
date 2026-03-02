"""kanban-tui backend for luskctl container orchestration.

Maps luskctl concepts to kanban-tui models with development workflow columns:
- Projects  -> Boards   (one board per luskctl project)
- Dev phases -> Columns  (Ready / Coding / Testing / Review / Done / Stopped)
- Tasks     -> Cards     (with live container state + agent work status)

All reads and writes go through the luskctl Python library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from kanban_tui.backends.base import Backend
from kanban_tui.classes.board import Board
from kanban_tui.classes.category import Category
from kanban_tui.classes.column import Column
from kanban_tui.classes.task import Task
from kanban_tui.config import LuskctlBackendSettings

from .data_reader import (
    WORK_STATUS_DISPLAY,
    PendingPhase,
    agent_config_dir,
    clear_pending_phase,
    get_all_task_states,
    get_tasks,
    list_projects,
    load_project,
    read_pending_phase,
    task_delete,
    task_followup_headless,
    task_new,
    task_rename,
    task_restart,
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


def _resolve_column(effective_status: str, work_status: str | None) -> int:
    """Map (effective_status, work_status) to a column ID.

    Infrastructure states (stopped, failed, created, deleting) always
    override work status.  When the container is running, work status
    picks the column.  Running with no status file defaults to Coding.
    """
    # Infrastructure states -> fixed columns
    if effective_status == "created":
        return 1  # Ready
    if effective_status in ("stopped", "not found", "deleting", "failed"):
        return 6  # Stopped
    if effective_status == "completed":
        return 5  # Done

    # Running -> use work status
    if effective_status == "running":
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

    Uses the luskctl Python library for all reads and writes.
    """

    settings: LuskctlBackendSettings

    # Internal caches — rebuilt on each get_boards() / get_tasks*() call.
    _project_id_to_board_id: dict[str, int] = field(default_factory=dict, repr=False)
    _board_id_to_project_id: dict[int, str] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self._refresh_projects()

    def _refresh_projects(self):
        """Discover projects via luskctl library and rebuild ID mappings."""
        self._projects = list_projects()
        self._project_id_to_board_id = {}
        self._board_id_to_project_id = {}
        for idx, p in enumerate(self._projects, start=1):
            self._project_id_to_board_id[p.id] = idx
            self._board_id_to_project_id[idx] = p.id

    def _project_for_board(self, board_id: int):
        """Return the project for a board, or None."""
        pid = self._board_id_to_project_id.get(board_id)
        if pid is None:
            return None
        for p in self._projects:
            if p.id == pid:
                return p
        return None

    def _active_project_id(self) -> str:
        """Return the active project ID, falling back to first project."""
        active = self.settings.active_project_id
        if active and any(p.id == active for p in self._projects):
            return active
        if self._projects:
            return self._projects[0].id
        return ""

    def _get_agent_config_dir(self, project_id: str, task_id: str):
        """Return the agent-config directory for a task."""
        project = load_project(project_id)
        return agent_config_dir(project, task_id)

    # === Board Management ===

    def get_boards(self) -> list[Board]:
        """Return one board per luskctl project."""
        self._refresh_projects()
        boards: list[Board] = []
        for proj in self._projects:
            bid = self._project_id_to_board_id[proj.id]
            icon = _SECURITY_ICONS.get(proj.security_class, "\U0001f4e6")
            try:
                ctime = proj.root.stat().st_ctime
                creation = datetime.fromtimestamp(ctime)
            except OSError:
                creation = datetime.now()
            boards.append(
                Board(
                    board_id=bid,
                    name=proj.id,
                    icon=icon,
                    creation_date=creation,
                    reset_column=1,  # Ready
                    start_column=2,  # Coding
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
        """Return summary info for all boards (read-only, no side-effects)."""
        boards = self.get_boards()
        infos = []
        for board in boards:
            pid = self._board_id_to_project_id.get(board.board_id)
            task_count = len(get_tasks(pid)) if pid else 0
            infos.append(
                {
                    "board_id": board.board_id,
                    "name": board.name,
                    "icon": board.icon,
                    "amount_tasks": task_count,
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
        task,
        effective_status: str,
    ) -> str:
        """Build rich description with emoji indicators for a task card."""
        parts: list[str] = []

        # Work status with emoji from luskctl's WORK_STATUS_DISPLAY
        ws_info = WORK_STATUS_DISPLAY.get(task.work_status or "")
        ws_emoji = ws_info.emoji if ws_info else ""

        if effective_status == "running":
            cs_emoji = "\U0001f7e2"
        elif effective_status == "failed":
            cs_emoji = "\U0001f534"
        else:
            cs_emoji = "\U0001f7e1"

        if task.work_status:
            parts.append(
                f"{ws_emoji} **{task.work_status.title()}** | {cs_emoji} {effective_status}"
            )
        else:
            parts.append(f"{cs_emoji} {effective_status.title()}")

        # Mode/backend/preset metadata
        meta_parts: list[str] = []
        if task.mode:
            meta_parts.append(f"Mode: {task.mode}")
        if task.backend:
            meta_parts.append(f"Backend: {task.backend}")
        if task.preset:
            meta_parts.append(f"Preset: {task.preset}")
        if meta_parts:
            parts.append(" | ".join(meta_parts))

        # Stopped column indicators
        if effective_status == "failed" and task.exit_code is not None:
            parts.append(f"\u274c Failed (exit code {task.exit_code})")
        elif task.work_status == "blocked":
            parts.append("\U0001f6a7 Agent reports: blocked")
            if task.work_message:
                parts.append(f"  {task.work_message}")
        elif task.work_status == "error":
            parts.append("\U0001f6ab Agent reports: error")
            if task.work_message:
                parts.append(f"  {task.work_message}")

        return "\n".join(parts)

    def _luskctl_to_kanban_task(
        self,
        task,
        project_id: str,
        effective_status: str,
        pending_phase: PendingPhase | None,
    ) -> Task:
        """Convert a luskctl TaskMeta to a kanban-tui Task."""
        column = _resolve_column(effective_status, task.work_status)

        # Infer dates from status
        now = datetime.now()
        start_date = now if effective_status in ("running", "completed") else None
        finish_date = now if effective_status == "completed" else None

        # Map mode to category
        category = _MODE_TO_CATEGORY.get(task.mode) if task.mode else None

        # Build description
        description = self._build_card_description(task, effective_status)

        # Pending phase indicator
        if pending_phase:
            description += f"\n\u23f3 Next phase: {pending_phase.phase.title()}"

        try:
            task_id = int(task.task_id)
        except (ValueError, TypeError):
            task_id = 0

        return Task(
            task_id=task_id,
            title=task.name or f"task-{task.task_id}",
            column=column,
            creation_date=now,
            start_date=start_date,
            finish_date=finish_date,
            category=category,
            description=description,
            metadata={
                "project_id": project_id,
                "mode": task.mode,
                "backend": task.backend,
                "preset": task.preset,
                "web_port": task.web_port,
                "container_status": effective_status,
                "work_status": task.work_status,
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

        project = load_project(pid)
        tasks = get_tasks(pid)
        if not tasks:
            return []

        # Batch query container states
        states = get_all_task_states(pid, tasks)

        # Enrich tasks with live container states
        for task in tasks:
            task.container_state = states.get(task.task_id, task.container_state)

        # Auto-execute pending phase transitions on stopped tasks
        for task in tasks:
            ac_dir = agent_config_dir(project, task.task_id)
            pp = read_pending_phase(ac_dir)
            if not pp:
                continue
            status = task.status  # uses effective_status() internally
            if status != "running":
                # Container is stopped — execute the pending phase
                if pp.phase == "done":
                    write_work_status(ac_dir, "done")
                    clear_pending_phase(ac_dir)
                elif pp.prompt and task.mode == "run":
                    write_work_status(ac_dir, pp.phase)
                    try:
                        task_followup_headless(
                            pid, task.task_id, prompt=pp.prompt, follow=False
                        )
                        clear_pending_phase(ac_dir)
                    except SystemExit:
                        pass  # leave pending-phase.yml for retry on next poll
                # Update fields so the card reflects the new state
                task.work_status = pp.phase

        # Build kanban tasks
        result: list[Task] = []
        for task in tasks:
            ac_dir = agent_config_dir(project, task.task_id)
            pp = read_pending_phase(ac_dir)
            kanban_task = self._luskctl_to_kanban_task(task, pid, task.status, pp)
            result.append(kanban_task)
        return result

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

    # === Write Operations (via luskctl library) ===

    def create_new_task(
        self,
        title: str,
        description: str,
        column: int,
        category: int | None = None,
        due_date: datetime | None = None,
    ) -> Task:
        """Create a new task via luskctl library."""
        pid = self._require_writable_project_id()
        task_id_str = task_new(pid, name=title)
        # Re-read the task from disk
        try:
            task = self.get_task_by_id(int(task_id_str))
        except (ValueError, TypeError):
            task = None
        if task is None:
            raise RuntimeError(f"Task {task_id_str} created but not found in state")
        return task

    def _require_writable_project_id(self) -> str:
        """Return the active project ID, raising if unavailable."""
        pid = self._active_project_id()
        if not pid:
            raise RuntimeError("No luskctl project available for write operation")
        return pid

    def delete_task(self, task_id: int):
        """Delete a task via luskctl library."""
        pid = self._require_writable_project_id()
        try:
            task_delete(pid, str(task_id))
        except SystemExit as exc:
            raise RuntimeError(f"Failed to delete task {task_id}") from exc

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

        ac_dir = self._get_agent_config_dir(pid, tid)

        if mode in ("cli", "web"):
            # Interactive tasks
            if container_status == "running":
                return  # Agent controls phase — blocked
            # Stopped interactive: only restart to Coding
            if target_col == 2:
                try:
                    task_restart(pid, tid)
                except SystemExit:
                    pass
            return

        # Autopilot (mode=run) tasks
        if container_status == "running":
            # Deferred: queue pending phase
            phase = _PHASE_WORK_STATUS.get(target_col, "coding")
            if target_col == 5:  # Done
                write_pending_phase(ac_dir, "done", "")
            else:
                prompt = _PHASE_PROMPTS.get(target_col, "")
                if prompt:
                    write_pending_phase(ac_dir, phase, prompt)
        else:
            # Stopped: execute immediately
            if target_col == 5:  # Done
                write_work_status(ac_dir, "done")
            elif target_col in _PHASE_PROMPTS:
                prompt = _PHASE_PROMPTS[target_col]
                write_work_status(ac_dir, _PHASE_WORK_STATUS[target_col])
                try:
                    task_followup_headless(pid, tid, prompt=prompt, follow=False)
                except SystemExit:
                    pass

    def update_task_entry(
        self,
        task_id: int,
        title: str,
        description: str,
        category: int | None,
        due_date: datetime | None,
    ) -> Task | None:
        """Rename a task via luskctl library."""
        pid = self._require_writable_project_id()
        try:
            task_rename(pid, str(task_id), title)
        except SystemExit as exc:
            raise RuntimeError(f"Failed to rename task {task_id}") from exc
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
        raise NotImplementedError("luskctl projects cannot be deleted from kanban-tui.")

    def update_board(self, board_id: int, name: str, icon: str):
        raise NotImplementedError("luskctl projects cannot be renamed from kanban-tui.")

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
