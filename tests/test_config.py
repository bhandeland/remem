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


def test_capture_model_defaults_to_sonnet():
    """Pinned rather than inherited from the session model.

    `claude -p` with no --model uses whatever the user's default is, so
    distillation cost and behaviour would change whenever they switch models
    for unrelated reasons. Measured on a real transcript: Haiku produced 1 of 3
    usable entries (a platitude, and a transient open question recorded as a
    durable rule); Sonnet and Opus produced 2 of 2. Distillation is a judgment
    task, not a compression one.
    """
    from remem.config import DEFAULT_CAPTURE_MODEL, load

    cfg = load(env={}, config_path=Path("/nonexistent"))
    assert cfg.capture_model == DEFAULT_CAPTURE_MODEL == "sonnet"


def test_capture_model_from_env():
    from remem.config import load

    cfg = load(env={"REMEM_CAPTURE_MODEL": "opus"}, config_path=Path("/nonexistent"))
    assert cfg.capture_model == "opus"


def test_capture_model_from_config_file(tmp_path):
    from remem.config import load

    p = tmp_path / "config.toml"
    p.write_text('capture_model = "haiku"\n')
    assert load(env={}, config_path=p).capture_model == "haiku"


def test_a_blank_capture_model_falls_back_to_the_default():
    """An empty value must not produce `--model ''`, which claude rejects."""
    from remem.config import DEFAULT_CAPTURE_MODEL, load

    cfg = load(env={"REMEM_CAPTURE_MODEL": "   "}, config_path=Path("/nonexistent"))
    assert cfg.capture_model == DEFAULT_CAPTURE_MODEL
