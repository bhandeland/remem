from pathlib import Path

from remem.config import DEFAULT_DSN, DEFAULT_HANDLE, DEFAULT_MAX_CHARS, load


def test_defaults_when_nothing_set():
    cfg = load(env={}, config_path=Path("/nonexistent/config.toml"))
    assert cfg.dsn == DEFAULT_DSN
    assert cfg.user_handle == DEFAULT_HANDLE
    assert cfg.max_chars == DEFAULT_MAX_CHARS


def test_config_file_overrides_defaults(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('dsn = "postgresql://x/y"\nuser_handle = "alice"\nmax_chars = 100\n')
    cfg = load(env={}, config_path=p)
    assert cfg.dsn == "postgresql://x/y"
    assert cfg.user_handle == "alice"
    assert cfg.max_chars == 100


def test_env_overrides_config_file(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('dsn = "postgresql://from/file"\nuser_handle = "alice"\n')
    cfg = load(
        env={"REMEM_DSN": "postgresql://from/env", "REMEM_USER_ID": "bob"},
        config_path=p,
    )
    assert cfg.dsn == "postgresql://from/env"
    assert cfg.user_handle == "bob"


def test_max_chars_from_env_is_an_int():
    cfg = load(env={"REMEM_MAX_CHARS": "1234"}, config_path=Path("/nonexistent"))
    assert cfg.max_chars == 1234


def test_malformed_config_file_does_not_crash(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("this is not = valid toml [[[")
    cfg = load(env={}, config_path=p)
    assert cfg.dsn == DEFAULT_DSN
