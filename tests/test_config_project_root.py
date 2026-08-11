from pathlib import Path

from uk_rent_agent.config import Config


def test_from_env_honours_explicit_container_project_root(monkeypatch, tmp_path):
    project_root = tmp_path / "runtime-root"
    (project_root / "app").mkdir(parents=True)
    monkeypatch.setenv("APP_PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("ENABLE_CHECKPOINTER", "0")
    monkeypatch.delenv("CHECKPOINT_DB_PATH", raising=False)
    monkeypatch.delenv("CHECKPOINT_PATH", raising=False)

    config = Config.from_env()

    assert config.project_root == project_root.resolve()
    assert config.data_dir == project_root.resolve() / "app" / "data"
    assert config.checkpoint_path == project_root.resolve() / ".runtime" / "checkpoints.sqlite3"


def test_from_env_keeps_source_checkout_default_without_override(monkeypatch):
    monkeypatch.delenv("APP_PROJECT_ROOT", raising=False)

    config = Config.from_env()

    assert config.project_root == Path(__file__).resolve().parents[1]
