"""Tests for the luskctl kanban-tui backend."""

from pathlib import Path

import pytest
import yaml

from kanban_tui.backends.luskctl.backend import (
    LuskctlBackend,
    _COLUMNS,
    _STATUS_TO_COLUMN,
)
from kanban_tui.config import LuskctlBackendSettings


def _setup_project(config_root: Path, state_root: Path, project_id: str, **proj_kw):
    """Create a minimal project config and return its root."""
    proj_dir = config_root / project_id
    proj_dir.mkdir(parents=True, exist_ok=True)
    cfg = {"project": {"id": project_id, "security_class": "online", **proj_kw}}
    (proj_dir / "project.yml").write_text(yaml.safe_dump(cfg))
    return proj_dir


def _setup_task(state_root: Path, project_id: str, task_id: str, **meta_kw):
    """Create a task metadata YAML file."""
    meta_dir = state_root / "projects" / project_id / "tasks"
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta = {"task_id": task_id, "name": f"task-{task_id}", **meta_kw}
    (meta_dir / f"{task_id}.yml").write_text(yaml.safe_dump(meta))


@pytest.fixture
def luskctl_env(tmp_path: Path):
    """Set up a luskctl-like filesystem environment."""
    config_root = tmp_path / "config"
    state_root = tmp_path / "state"
    config_root.mkdir()
    state_root.mkdir()
    return config_root, state_root


@pytest.fixture
def single_project_backend(luskctl_env):
    """Backend with one project and three tasks."""
    config_root, state_root = luskctl_env

    _setup_project(config_root, state_root, "myproj")
    _setup_task(state_root, "myproj", "1", mode=None)  # created
    _setup_task(state_root, "myproj", "2", mode="cli", exit_code=0)  # completed
    _setup_task(state_root, "myproj", "3", mode="run", preset="solo")  # not found

    settings = LuskctlBackendSettings(
        state_root=str(state_root),
        config_root=str(config_root),
        active_project_id="myproj",
    )
    return LuskctlBackend(settings)


class TestBoardManagement:
    def test_get_boards(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "alpha")
        _setup_project(config_root, state_root, "beta")

        settings = LuskctlBackendSettings(
            state_root=str(state_root), config_root=str(config_root)
        )
        backend = LuskctlBackend(settings)

        boards = backend.get_boards()
        assert len(boards) == 2
        assert boards[0].name == "alpha"
        assert boards[1].name == "beta"
        assert boards[0].icon == "🌐"  # online

    def test_gatekeeping_icon(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(
            config_root, state_root, "secure", security_class="gatekeeping"
        )

        settings = LuskctlBackendSettings(
            state_root=str(state_root), config_root=str(config_root)
        )
        backend = LuskctlBackend(settings)
        boards = backend.get_boards()
        assert boards[0].icon == "🔒"

    def test_active_board(self, single_project_backend):
        board = single_project_backend.active_board
        assert board.name == "myproj"

    def test_active_board_fallback_to_first(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "first")

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="nonexistent",
        )
        backend = LuskctlBackend(settings)
        board = backend.active_board
        assert board.name == "first"

    def test_no_projects_raises(self, luskctl_env):
        config_root, state_root = luskctl_env
        settings = LuskctlBackendSettings(
            state_root=str(state_root), config_root=str(config_root)
        )
        backend = LuskctlBackend(settings)
        with pytest.raises(Exception, match="No luskctl projects found"):
            _ = backend.active_board

    def test_board_infos(self, single_project_backend):
        infos = single_project_backend.get_board_infos()
        assert len(infos) == 1
        assert infos[0]["name"] == "myproj"
        assert infos[0]["amount_tasks"] == 3
        assert infos[0]["amount_columns"] == len(_COLUMNS)

    def test_board_columns_set(self, single_project_backend):
        board = single_project_backend.active_board
        assert board.reset_column == 1  # Created
        assert board.start_column == 2  # Running
        assert board.finish_column == 4  # Completed


class TestColumnManagement:
    def test_get_columns(self, single_project_backend):
        columns = single_project_backend.get_columns()
        assert len(columns) == 5
        names = [c.name for c in columns]
        assert names == ["Created", "Running", "Stopped", "Completed", "Failed"]

    def test_get_column_by_id(self, single_project_backend):
        col = single_project_backend.get_column_by_id(2)
        assert col is not None
        assert col.name == "Running"

    def test_get_column_by_id_nonexistent(self, single_project_backend):
        assert single_project_backend.get_column_by_id(99) is None


class TestTaskManagement:
    def test_get_tasks_on_active_board(self, single_project_backend):
        """Tasks are read from YAML; container states are empty (no podman)."""
        tasks = single_project_backend.get_tasks_on_active_board()
        assert len(tasks) == 3

    def test_task_status_mapping_created(self, single_project_backend):
        tasks = single_project_backend.get_tasks_on_active_board()
        task1 = next(t for t in tasks if t.task_id == 1)
        # mode=None, no container -> "created" -> column 1
        assert task1.column == _STATUS_TO_COLUMN["created"]

    def test_task_status_mapping_completed(self, single_project_backend):
        tasks = single_project_backend.get_tasks_on_active_board()
        task2 = next(t for t in tasks if t.task_id == 2)
        # mode="cli", exit_code=0, no container -> "completed" -> column 4
        assert task2.column == _STATUS_TO_COLUMN["completed"]

    def test_task_status_mapping_not_found(self, single_project_backend):
        tasks = single_project_backend.get_tasks_on_active_board()
        task3 = next(t for t in tasks if t.task_id == 3)
        # mode="run", no exit_code, no container -> "not found" -> column 3 (stopped)
        assert task3.column == _STATUS_TO_COLUMN["not found"]

    def test_task_metadata(self, single_project_backend):
        tasks = single_project_backend.get_tasks_on_active_board()
        task3 = next(t for t in tasks if t.task_id == 3)
        assert task3.metadata["project_id"] == "myproj"
        assert task3.metadata["mode"] == "run"
        assert task3.metadata["preset"] == "solo"
        assert task3.metadata["source"] == "luskctl"

    def test_task_title_from_name(self, single_project_backend):
        tasks = single_project_backend.get_tasks_on_active_board()
        task1 = next(t for t in tasks if t.task_id == 1)
        assert task1.title == "task-1"

    def test_get_task_by_id(self, single_project_backend):
        task = single_project_backend.get_task_by_id(2)
        assert task is not None
        assert task.task_id == 2

    def test_get_task_by_id_nonexistent(self, single_project_backend):
        assert single_project_backend.get_task_by_id(999) is None

    def test_get_tasks_by_ids(self, single_project_backend):
        tasks = single_project_backend.get_tasks_by_ids([1, 3])
        assert len(tasks) == 2
        ids = {t.task_id for t in tasks}
        assert ids == {1, 3}

    def test_empty_project_returns_no_tasks(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "empty")

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="empty",
        )
        backend = LuskctlBackend(settings)
        assert backend.get_tasks_on_active_board() == []


class TestCategoryManagement:
    def test_get_all_categories(self, single_project_backend):
        cats = single_project_backend.get_all_categories()
        assert len(cats) == 3
        names = [c.name for c in cats]
        assert "CLI" in names
        assert "Web" in names
        assert "Autopilot" in names

    def test_get_category_by_id(self, single_project_backend):
        cat = single_project_backend.get_category_by_id(1)
        assert cat.name == "CLI"


class TestNotImplementedOperations:
    def test_create_board_raises(self, single_project_backend):
        with pytest.raises(NotImplementedError):
            single_project_backend.create_new_board("Test")

    def test_delete_board_raises(self, single_project_backend):
        with pytest.raises(NotImplementedError):
            single_project_backend.delete_board(1)

    def test_update_board_raises(self, single_project_backend):
        with pytest.raises(NotImplementedError):
            single_project_backend.update_board(1, "name", "icon")

    def test_create_category_raises(self, single_project_backend):
        with pytest.raises(NotImplementedError):
            single_project_backend.create_new_category("Test", "#fff")

    def test_update_column_visibility_raises(self, single_project_backend):
        with pytest.raises(NotImplementedError):
            single_project_backend.update_column_visibility(1, False)

    def test_create_dependency_raises(self, single_project_backend):
        with pytest.raises(NotImplementedError):
            single_project_backend.create_task_dependency(1, 2)

    def test_would_create_cycle_returns_false(self, single_project_backend):
        assert single_project_backend.would_create_dependency_cycle(1, 2) is False


class TestTaskDescriptionFormatting:
    def test_description_includes_mode_and_preset(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "proj")
        _setup_task(
            state_root,
            "proj",
            "1",
            mode="run",
            backend="claude",
            preset="team",
            web_port=None,
        )

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="proj",
        )
        backend = LuskctlBackend(settings)
        tasks = backend.get_tasks_on_active_board()
        assert "Mode: run" in tasks[0].description
        assert "Backend: claude" in tasks[0].description
        assert "Preset: team" in tasks[0].description

    def test_description_includes_web_port(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "proj")
        _setup_task(
            state_root, "proj", "1", mode="web", backend="claude", web_port=7860
        )

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="proj",
        )
        backend = LuskctlBackend(settings)
        tasks = backend.get_tasks_on_active_board()
        assert "Port: 7860" in tasks[0].description

    def test_mode_category_mapping(self, luskctl_env):
        config_root, state_root = luskctl_env
        _setup_project(config_root, state_root, "proj")
        _setup_task(state_root, "proj", "1", mode="cli")
        _setup_task(state_root, "proj", "2", mode="web")
        _setup_task(state_root, "proj", "3", mode="run")
        _setup_task(state_root, "proj", "4", mode=None)

        settings = LuskctlBackendSettings(
            state_root=str(state_root),
            config_root=str(config_root),
            active_project_id="proj",
        )
        backend = LuskctlBackend(settings)
        tasks = backend.get_tasks_on_active_board()

        task_cats = {t.task_id: t.category for t in tasks}
        assert task_cats[1] == 1  # CLI
        assert task_cats[2] == 2  # Web
        assert task_cats[3] == 3  # Autopilot
        assert task_cats[4] is None  # No mode
