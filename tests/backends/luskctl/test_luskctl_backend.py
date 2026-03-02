"""Tests for the luskctl kanban-tui backend with development workflow columns.

All luskctl library functions are mocked — tests verify backend logic only.
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kanban_tui.backends.luskctl.backend import (
    LuskctlBackend,
    _COLUMNS,
    _PHASE_PROMPTS,
    _resolve_column,
)
from kanban_tui.classes.task import Task
from kanban_tui.config import LuskctlBackendSettings

_NOW = datetime.now()

# ---------- Helpers ----------

# Patch target: the consuming module, not the provider (data_reader).
# backend.py does `from .data_reader import list_projects`, which creates a
# local binding.  We must patch where the name is looked up at call time.
_B = "kanban_tui.backends.luskctl.backend"


def _make_project(pid="myproj", security_class="online", root="/tmp/proj"):
    """Create a mock Project object."""
    p = MagicMock()
    p.id = pid
    p.security_class = security_class
    p.root = Path(root)
    p.tasks_root = Path(root) / "tasks"
    return p


def _make_task_meta(
    task_id="1",
    name="",
    mode=None,
    workspace="",
    web_port=None,
    backend=None,
    container_state=None,
    exit_code=None,
    deleting=False,
    preset=None,
    work_status=None,
    work_message=None,
):
    """Create a mock TaskMeta object."""
    t = MagicMock()
    t.task_id = task_id
    t.name = name or f"task-{task_id}"
    t.mode = mode
    t.workspace = workspace
    t.web_port = web_port
    t.backend = backend
    t.container_state = container_state
    t.exit_code = exit_code
    t.deleting = deleting
    t.preset = preset
    t.work_status = work_status
    t.work_message = work_message
    # status property uses effective_status logic
    if deleting:
        t.status = "deleting"
    elif container_state == "running":
        t.status = "running"
    elif container_state is not None:
        if exit_code == 0:
            t.status = "completed"
        elif exit_code is not None:
            t.status = "failed"
        else:
            t.status = "stopped"
    elif mode is None:
        t.status = "created"
    elif exit_code == 0:
        t.status = "completed"
    elif exit_code is not None:
        t.status = "failed"
    else:
        t.status = "not found"
    return t


@pytest.fixture
def mock_luskctl():
    """Patch all luskctl library functions used by the backend."""
    proj = _make_project()
    with (
        patch(f"{_B}.list_projects", return_value=[proj]) as m_list,
        patch(f"{_B}.load_project", return_value=proj) as m_load,
        patch(f"{_B}.get_tasks", return_value=[]) as m_tasks,
        patch(f"{_B}.get_all_task_states", return_value={}) as m_states,
        patch(f"{_B}.read_pending_phase", return_value=None) as m_rpp,
        patch(f"{_B}.write_work_status") as m_wws,
        patch(f"{_B}.write_pending_phase") as m_wpp,
        patch(f"{_B}.clear_pending_phase") as m_cpp,
        patch(f"{_B}.task_new", return_value="42") as m_tnew,
        patch(f"{_B}.task_delete") as m_tdel,
        patch(f"{_B}.task_rename") as m_tren,
        patch(f"{_B}.task_restart") as m_trestart,
        patch(f"{_B}.task_followup_headless") as m_tfollowup,
    ):
        yield {
            "list_projects": m_list,
            "load_project": m_load,
            "get_tasks": m_tasks,
            "get_all_task_states": m_states,
            "read_pending_phase": m_rpp,
            "write_work_status": m_wws,
            "write_pending_phase": m_wpp,
            "clear_pending_phase": m_cpp,
            "task_new": m_tnew,
            "task_delete": m_tdel,
            "task_rename": m_tren,
            "task_restart": m_trestart,
            "task_followup_headless": m_tfollowup,
        }


@pytest.fixture
def backend(mock_luskctl):
    """Create a backend with a single mocked project."""
    settings = LuskctlBackendSettings(active_project_id="myproj")
    return LuskctlBackend(settings)


# ---------- Column resolution ----------


class TestColumnResolution:
    def test_created_goes_to_ready(self):
        assert _resolve_column("created", None) == 1

    def test_running_no_status_goes_to_coding(self):
        assert _resolve_column("running", None) == 2

    def test_running_coding_goes_to_coding(self):
        assert _resolve_column("running", "coding") == 2

    def test_running_planning_goes_to_coding(self):
        assert _resolve_column("running", "planning") == 2

    def test_running_debugging_goes_to_coding(self):
        assert _resolve_column("running", "debugging") == 2

    def test_running_testing_goes_to_testing(self):
        assert _resolve_column("running", "testing") == 3

    def test_running_reviewing_goes_to_review(self):
        assert _resolve_column("running", "reviewing") == 4

    def test_running_documenting_goes_to_review(self):
        assert _resolve_column("running", "documenting") == 4

    def test_running_done_goes_to_done(self):
        assert _resolve_column("running", "done") == 5

    def test_running_blocked_goes_to_stopped(self):
        assert _resolve_column("running", "blocked") == 6

    def test_running_error_goes_to_stopped(self):
        assert _resolve_column("running", "error") == 6

    def test_completed_goes_to_done(self):
        assert _resolve_column("completed", None) == 5

    def test_stopped_goes_to_stopped(self):
        assert _resolve_column("stopped", None) == 6

    def test_failed_goes_to_stopped(self):
        assert _resolve_column("failed", None) == 6

    def test_not_found_goes_to_stopped(self):
        assert _resolve_column("not found", None) == 6

    def test_deleting_goes_to_stopped(self):
        assert _resolve_column("deleting", None) == 6

    def test_running_unknown_status_goes_to_coding(self):
        assert _resolve_column("running", "thinking-hard") == 2


# ---------- Board management ----------


class TestBoardManagement:
    def test_get_boards(self, mock_luskctl):
        proj_a = _make_project("alpha", root="/tmp/alpha")
        proj_b = _make_project("beta", root="/tmp/beta")
        mock_luskctl["list_projects"].return_value = [proj_a, proj_b]

        settings = LuskctlBackendSettings()
        be = LuskctlBackend(settings)
        boards = be.get_boards()
        assert len(boards) == 2
        assert boards[0].name == "alpha"
        assert boards[1].name == "beta"

    def test_gatekeeping_icon(self, mock_luskctl):
        proj = _make_project("secure", security_class="gatekeeping")
        mock_luskctl["list_projects"].return_value = [proj]

        settings = LuskctlBackendSettings()
        be = LuskctlBackend(settings)
        boards = be.get_boards()
        assert boards[0].icon == "\U0001f512"

    def test_active_board(self, backend):
        board = backend.active_board
        assert board.name == "myproj"

    def test_active_board_fallback_to_first(self, mock_luskctl):
        proj = _make_project("first")
        mock_luskctl["list_projects"].return_value = [proj]

        settings = LuskctlBackendSettings(active_project_id="nonexistent")
        be = LuskctlBackend(settings)
        board = be.active_board
        assert board.name == "first"

    def test_stale_active_project_fallback_used_for_create(self, mock_luskctl):
        proj = _make_project("first")
        mock_luskctl["list_projects"].return_value = [proj]
        be = LuskctlBackend(LuskctlBackendSettings(active_project_id="nonexistent"))

        mock_luskctl["task_new"].return_value = "42"
        mock_luskctl["get_tasks"].return_value = [_make_task_meta("42", mode=None)]

        be.create_new_task("my-task", "desc", column=1)
        mock_luskctl["task_new"].assert_called_once_with("first", name="my-task")

    def test_no_projects_raises(self, mock_luskctl):
        mock_luskctl["list_projects"].return_value = []

        settings = LuskctlBackendSettings()
        be = LuskctlBackend(settings)
        with pytest.raises(Exception, match="No luskctl projects found"):
            _ = be.active_board

    def test_board_infos(self, backend, mock_luskctl):
        mock_luskctl["get_tasks"].return_value = [
            _make_task_meta("1"),
            _make_task_meta("2"),
        ]
        infos = backend.get_board_infos()
        assert len(infos) == 1
        assert infos[0]["name"] == "myproj"
        assert infos[0]["amount_tasks"] == 2
        assert infos[0]["amount_columns"] == len(_COLUMNS)

    def test_board_infos_is_read_only(self, backend, mock_luskctl):
        mock_luskctl["get_tasks"].return_value = [_make_task_meta("1")]
        backend.get_board_infos()
        mock_luskctl["get_all_task_states"].assert_not_called()
        mock_luskctl["read_pending_phase"].assert_not_called()
        mock_luskctl["write_work_status"].assert_not_called()

    def test_board_markers(self, backend):
        board = backend.active_board
        assert board.reset_column == 1  # Ready
        assert board.start_column == 2  # Coding
        assert board.finish_column == 5  # Done


# ---------- Column management ----------


class TestColumnManagement:
    def test_get_columns_returns_six(self, backend):
        columns = backend.get_columns()
        assert len(columns) == 6
        names = [c.name for c in columns]
        assert names == ["Ready", "Coding", "Testing", "Review", "Done", "Stopped"]

    def test_get_column_by_id(self, backend):
        col = backend.get_column_by_id(3)
        assert col is not None
        assert col.name == "Testing"

    def test_get_column_by_id_nonexistent(self, backend):
        assert backend.get_column_by_id(99) is None


# ---------- Task management ----------


class TestTaskManagement:
    def test_get_tasks_on_active_board(self, backend, mock_luskctl):
        mock_luskctl["get_tasks"].return_value = [
            _make_task_meta("1", mode=None),
            _make_task_meta("2", mode="cli", exit_code=0),
            _make_task_meta("3", mode="run", preset="solo"),
        ]
        tasks = backend.get_tasks_on_active_board()
        assert len(tasks) == 3

    def test_created_task_in_ready_column(self, backend, mock_luskctl):
        mock_luskctl["get_tasks"].return_value = [
            _make_task_meta("1", mode=None),
        ]
        tasks = backend.get_tasks_on_active_board()
        assert tasks[0].column == 1

    def test_completed_task_in_done_column(self, backend, mock_luskctl):
        mock_luskctl["get_tasks"].return_value = [
            _make_task_meta("2", mode="cli", exit_code=0),
        ]
        tasks = backend.get_tasks_on_active_board()
        assert tasks[0].column == 5

    def test_not_found_task_in_stopped_column(self, backend, mock_luskctl):
        mock_luskctl["get_tasks"].return_value = [
            _make_task_meta("3", mode="run", preset="solo"),
        ]
        tasks = backend.get_tasks_on_active_board()
        assert tasks[0].column == 6

    def test_task_metadata_includes_container_status(self, backend, mock_luskctl):
        mock_luskctl["get_tasks"].return_value = [
            _make_task_meta("3", mode="run", preset="solo"),
        ]
        tasks = backend.get_tasks_on_active_board()
        assert tasks[0].metadata["project_id"] == "myproj"
        assert tasks[0].metadata["mode"] == "run"
        assert tasks[0].metadata["preset"] == "solo"
        assert tasks[0].metadata["source"] == "luskctl"
        assert tasks[0].metadata["container_status"] == "not found"

    def test_missing_state_entry_preserves_existing_container_state(
        self, backend, mock_luskctl
    ):
        meta = _make_task_meta("1", mode="run", container_state="running")
        mock_luskctl["get_tasks"].return_value = [meta]
        mock_luskctl["get_all_task_states"].return_value = {}  # missing key

        tasks = backend.get_tasks_on_active_board()
        assert len(tasks) == 1
        assert tasks[0].metadata["container_status"] == "running"
        assert tasks[0].column == 2  # running defaults to Coding

    def test_task_title_from_name(self, backend, mock_luskctl):
        mock_luskctl["get_tasks"].return_value = [
            _make_task_meta("1", mode=None, name="happy-hawk"),
        ]
        tasks = backend.get_tasks_on_active_board()
        assert tasks[0].title == "happy-hawk"

    def test_get_task_by_id(self, backend, mock_luskctl):
        mock_luskctl["get_tasks"].return_value = [
            _make_task_meta("2", mode="cli", exit_code=0),
        ]
        task = backend.get_task_by_id(2)
        assert task is not None
        assert task.task_id == 2

    def test_get_task_by_id_nonexistent(self, backend, mock_luskctl):
        mock_luskctl["get_tasks"].return_value = []
        assert backend.get_task_by_id(999) is None

    def test_get_tasks_by_ids(self, backend, mock_luskctl):
        mock_luskctl["get_tasks"].return_value = [
            _make_task_meta("1", mode=None),
            _make_task_meta("2", mode="cli", exit_code=0),
            _make_task_meta("3", mode="run"),
        ]
        tasks = backend.get_tasks_by_ids([1, 3])
        assert len(tasks) == 2
        ids = {t.task_id for t in tasks}
        assert ids == {1, 3}

    def test_empty_project_returns_no_tasks(self, backend, mock_luskctl):
        mock_luskctl["get_tasks"].return_value = []
        assert backend.get_tasks_on_active_board() == []

    def test_malformed_task_id_skipped(self, backend, mock_luskctl):
        """int(meta.task_id) ValueError is handled gracefully."""
        meta = _make_task_meta("abc", mode=None)
        mock_luskctl["get_tasks"].return_value = [meta]
        tasks = backend.get_tasks_on_active_board()
        # task_id=0 for non-numeric IDs
        assert len(tasks) == 1
        assert tasks[0].task_id == 0


# ---------- Work status in cards ----------


class TestWorkStatusInCards:
    def test_work_status_in_metadata(self, backend, mock_luskctl):
        mock_luskctl["get_tasks"].return_value = [
            _make_task_meta("1", mode="run", work_status="testing"),
        ]
        tasks = backend.get_tasks_on_active_board()
        assert tasks[0].metadata["work_status"] == "testing"


# ---------- Card descriptions ----------


class TestCardDescriptions:
    def test_stopped_task_shows_status(self, backend, mock_luskctl):
        mock_luskctl["get_tasks"].return_value = [
            _make_task_meta("1", mode="run", exit_code=1),
        ]
        tasks = backend.get_tasks_on_active_board()
        assert "Failed (exit code 1)" in tasks[0].description

    def test_blocked_indicator(self, backend, mock_luskctl):
        mock_luskctl["get_tasks"].return_value = [
            _make_task_meta(
                "1", mode="run", work_status="blocked", work_message="Need API key"
            ),
        ]
        tasks = backend.get_tasks_on_active_board()
        assert "Agent reports: blocked" in tasks[0].description
        assert "Need API key" in tasks[0].description

    def test_error_uses_prohibited_emoji(self, backend, mock_luskctl):
        """Error status uses U+1F6AB (prohibited) not VS16 sequence."""
        mock_luskctl["get_tasks"].return_value = [
            _make_task_meta("1", mode="run", work_status="error", work_message="Oops"),
        ]
        tasks = backend.get_tasks_on_active_board()
        assert "\U0001f6ab" in tasks[0].description
        # Must NOT contain VS16 (U+FE0F)
        assert "\ufe0f" not in tasks[0].description

    def test_mode_category_mapping(self, backend, mock_luskctl):
        mock_luskctl["get_tasks"].return_value = [
            _make_task_meta("1", mode="cli"),
            _make_task_meta("2", mode="web"),
            _make_task_meta("3", mode="run"),
            _make_task_meta("4", mode=None),
        ]
        tasks = backend.get_tasks_on_active_board()
        task_cats = {t.task_id: t.category for t in tasks}
        assert task_cats[1] == 1  # CLI
        assert task_cats[2] == 2  # Web
        assert task_cats[3] == 3  # Autopilot
        assert task_cats[4] is None  # No mode


# ---------- Deferred phase transitions ----------


class TestDeferredPhaseTransition:
    def test_running_autopilot_writes_pending_phase(self, backend, mock_luskctl):
        task = Task(
            task_id=1,
            title="task-1",
            creation_date=_NOW,
            column=3,  # Testing
            metadata={
                "project_id": "myproj",
                "mode": "run",
                "container_status": "running",
            },
        )
        backend.update_task_status(task)
        mock_luskctl["write_pending_phase"].assert_called_once()
        call_args = mock_luskctl["write_pending_phase"].call_args
        assert call_args[0][1] == "testing"
        assert call_args[0][2] == _PHASE_PROMPTS[3]

    def test_running_autopilot_done_writes_pending_done(self, backend, mock_luskctl):
        task = Task(
            task_id=1,
            title="task-1",
            creation_date=_NOW,
            column=5,  # Done
            metadata={
                "project_id": "myproj",
                "mode": "run",
                "container_status": "running",
            },
        )
        backend.update_task_status(task)
        mock_luskctl["write_pending_phase"].assert_called_once()
        call_args = mock_luskctl["write_pending_phase"].call_args
        assert call_args[0][1] == "done"


# ---------- Immediate phase transitions ----------


class TestImmediatePhaseTransition:
    def test_stopped_autopilot_calls_followup(self, backend, mock_luskctl):
        task = Task(
            task_id=1,
            title="task-1",
            creation_date=_NOW,
            column=3,  # Testing
            metadata={
                "project_id": "myproj",
                "mode": "run",
                "container_status": "stopped",
            },
        )
        backend.update_task_status(task)

        mock_luskctl["task_followup_headless"].assert_called_once_with(
            "myproj", "1", prompt=_PHASE_PROMPTS[3], follow=False
        )
        mock_luskctl["write_work_status"].assert_called_once()

    def test_stopped_autopilot_done_writes_status(self, backend, mock_luskctl):
        task = Task(
            task_id=1,
            title="task-1",
            creation_date=_NOW,
            column=5,  # Done
            metadata={
                "project_id": "myproj",
                "mode": "run",
                "container_status": "stopped",
            },
        )
        backend.update_task_status(task)
        mock_luskctl["write_work_status"].assert_called_once()
        call_args = mock_luskctl["write_work_status"].call_args
        assert call_args[0][1] == "done"


# ---------- Auto-execution of pending phases ----------


class TestPendingPhaseAutoExecution:
    def test_stopped_with_pending_triggers_followup(self, backend, mock_luskctl):
        pp = MagicMock()
        pp.phase = "testing"
        pp.prompt = "Run tests"

        task_meta = _make_task_meta("1", mode="run")
        mock_luskctl["get_tasks"].return_value = [task_meta]
        mock_luskctl["read_pending_phase"].return_value = pp

        tasks = backend.get_tasks_on_active_board()
        assert len(tasks) == 1

        mock_luskctl["task_followup_headless"].assert_called_once()
        mock_luskctl["clear_pending_phase"].assert_called_once()

    def test_stopped_with_pending_done_writes_status(self, backend, mock_luskctl):
        pp = MagicMock()
        pp.phase = "done"
        pp.prompt = ""

        task_meta = _make_task_meta("1", mode="run")
        mock_luskctl["get_tasks"].return_value = [task_meta]
        mock_luskctl["read_pending_phase"].return_value = pp

        tasks = backend.get_tasks_on_active_board()
        assert len(tasks) == 1

        mock_luskctl["write_work_status"].assert_called()

    def test_pending_phase_failure_leaves_file(self, backend, mock_luskctl):
        """Regression: pending-phase.yml should NOT be cleared when followup fails."""
        pp = MagicMock()
        pp.phase = "testing"
        pp.prompt = "Run tests"

        task_meta = _make_task_meta("1", mode="run")
        mock_luskctl["get_tasks"].return_value = [task_meta]
        mock_luskctl["read_pending_phase"].return_value = pp
        mock_luskctl["task_followup_headless"].side_effect = SystemExit(1)

        tasks = backend.get_tasks_on_active_board()
        assert len(tasks) == 1

        # followup was attempted
        mock_luskctl["task_followup_headless"].assert_called_once()
        # pending phase was NOT cleared (failure path)
        mock_luskctl["clear_pending_phase"].assert_not_called()


# ---------- Interactive task blocking ----------


class TestInteractiveTaskBlocking:
    def test_running_interactive_all_moves_blocked(self, backend, mock_luskctl):
        task = Task(
            task_id=1,
            title="task-1",
            creation_date=_NOW,
            column=3,
            metadata={
                "project_id": "myproj",
                "mode": "cli",
                "container_status": "running",
            },
        )
        backend.update_task_status(task)
        # No writes should have been made
        mock_luskctl["write_pending_phase"].assert_not_called()
        mock_luskctl["write_work_status"].assert_not_called()

    def test_stopped_interactive_restart_to_coding(self, backend, mock_luskctl):
        task = Task(
            task_id=1,
            title="task-1",
            creation_date=_NOW,
            column=2,  # Coding
            metadata={
                "project_id": "myproj",
                "mode": "web",
                "container_status": "stopped",
            },
        )
        backend.update_task_status(task)
        mock_luskctl["task_restart"].assert_called_once_with("myproj", "1")


# ---------- Unstarted task blocking ----------


class TestUnstartedTaskBlocking:
    def test_unstarted_all_moves_blocked(self, backend, mock_luskctl):
        for target_col in (2, 3, 4, 5):
            task = Task(
                task_id=1,
                title="task-1",
                creation_date=_NOW,
                column=target_col,
                metadata={
                    "project_id": "myproj",
                    "mode": None,
                    "container_status": "created",
                },
            )
            backend.update_task_status(task)
        # No writes should have been made
        mock_luskctl["write_pending_phase"].assert_not_called()
        mock_luskctl["write_work_status"].assert_not_called()
        mock_luskctl["task_followup_headless"].assert_not_called()


# ---------- Invalid move blocking ----------


class TestInvalidMoveBlocking:
    def test_move_to_ready_blocked(self, backend, mock_luskctl):
        task = Task(
            task_id=1,
            title="task-1",
            creation_date=_NOW,
            column=1,  # Ready
            metadata={
                "project_id": "myproj",
                "mode": "run",
                "container_status": "stopped",
            },
        )
        backend.update_task_status(task)
        mock_luskctl["write_pending_phase"].assert_not_called()

    def test_move_to_stopped_blocked(self, backend, mock_luskctl):
        task = Task(
            task_id=1,
            title="task-1",
            creation_date=_NOW,
            column=6,  # Stopped
            metadata={
                "project_id": "myproj",
                "mode": "run",
                "container_status": "running",
            },
        )
        backend.update_task_status(task)
        mock_luskctl["write_pending_phase"].assert_not_called()


# ---------- Write operations ----------


class TestWriteOperations:
    def test_create_new_task(self, backend, mock_luskctl):
        # After task_new returns "42", get_tasks should return the new task
        new_meta = _make_task_meta("42", mode=None, name="my-task")
        mock_luskctl["get_tasks"].return_value = [new_meta]

        task = backend.create_new_task("my-task", "desc", column=1)
        mock_luskctl["task_new"].assert_called_once_with("myproj", name="my-task")
        assert task.task_id == 42

    def test_delete_task(self, backend, mock_luskctl):
        backend.delete_task(1)
        mock_luskctl["task_delete"].assert_called_once_with("myproj", "1")

    def test_delete_task_system_exit_raises_runtime_error(self, backend, mock_luskctl):
        mock_luskctl["task_delete"].side_effect = SystemExit(1)
        with pytest.raises(RuntimeError, match="Failed to delete task 1"):
            backend.delete_task(1)

    def test_update_task_entry_renames(self, backend, mock_luskctl):
        meta = _make_task_meta("1", name="new-name", mode=None)
        mock_luskctl["get_tasks"].return_value = [meta]

        backend.update_task_entry(1, "new-name", "desc", None, None)
        mock_luskctl["task_rename"].assert_called_once_with("myproj", "1", "new-name")


# ---------- Category management ----------


class TestCategoryManagement:
    def test_get_all_categories(self, backend):
        cats = backend.get_all_categories()
        assert len(cats) == 3
        names = [c.name for c in cats]
        assert "CLI" in names
        assert "Web" in names
        assert "Autopilot" in names

    def test_get_category_by_id(self, backend):
        cat = backend.get_category_by_id(1)
        assert cat.name == "CLI"


# ---------- Not implemented operations ----------


class TestNotImplementedOperations:
    def test_create_board_raises(self, backend):
        with pytest.raises(NotImplementedError):
            backend.create_new_board("Test")

    def test_delete_board_raises(self, backend):
        with pytest.raises(NotImplementedError):
            backend.delete_board(1)

    def test_update_board_raises(self, backend):
        with pytest.raises(NotImplementedError):
            backend.update_board(1, "name", "icon")

    def test_create_category_raises(self, backend):
        with pytest.raises(NotImplementedError):
            backend.create_new_category("Test", "#fff")

    def test_update_column_visibility_raises(self, backend):
        with pytest.raises(NotImplementedError):
            backend.update_column_visibility(1, False)

    def test_create_dependency_raises(self, backend):
        with pytest.raises(NotImplementedError):
            backend.create_task_dependency(1, 2)

    def test_would_create_cycle_returns_false(self, backend):
        assert backend.would_create_dependency_cycle(1, 2) is False
