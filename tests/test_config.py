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


def test_extract_model_defaults_to_sonnet():
    """Pinned rather than inherited from the session model.

    `claude -p` with no --model uses whatever the user's default is, so
    extraction cost and behaviour would change whenever they switch models
    for unrelated reasons. Measured on a real transcript: Haiku produced 1 of 3
    usable entries (a platitude, and a transient open question recorded as a
    durable rule); Sonnet and Opus produced 2 of 2. Extraction is a judgment
    task, not a compression one.
    """
    from remem.config import DEFAULT_EXTRACT_MODEL, load

    cfg = load(env={}, config_path=Path("/nonexistent"))
    assert cfg.extract_model == DEFAULT_EXTRACT_MODEL == "sonnet"


def test_extract_model_from_env():
    from remem.config import load

    cfg = load(env={"REMEM_EXTRACT_MODEL": "opus"}, config_path=Path("/nonexistent"))
    assert cfg.extract_model == "opus"


def test_extract_model_from_config_file(tmp_path):
    from remem.config import load

    p = tmp_path / "config.toml"
    p.write_text('extract_model = "haiku"\n')
    assert load(env={}, config_path=p).extract_model == "haiku"


def test_a_blank_extract_model_falls_back_to_the_default():
    """An empty value must not produce `--model ''`, which claude rejects."""
    from remem.config import DEFAULT_EXTRACT_MODEL, load

    cfg = load(env={"REMEM_EXTRACT_MODEL": "   "}, config_path=Path("/nonexistent"))
    assert cfg.extract_model == DEFAULT_EXTRACT_MODEL


def test_turn_thresholds_come_from_the_environment(tmp_path):
    cfg = load(env={"REMEM_TURN_WARN_AT": "80", "REMEM_TURN_WARN_EVERY": "20"},
               config_path=tmp_path / "none.toml")
    assert cfg.turn_warn_at == 80
    assert cfg.turn_warn_every == 20


def test_nonsense_turn_thresholds_fall_back_to_the_defaults(tmp_path):
    cfg = load(env={"REMEM_TURN_WARN_AT": "zero", "REMEM_TURN_WARN_EVERY": "0"},
               config_path=tmp_path / "none.toml")
    assert cfg.turn_warn_at == 150
    assert cfg.turn_warn_every == 50


def test_idle_minutes_defaults_to_twenty(tmp_path):
    from remem.config import DEFAULT_IDLE_MINUTES, load

    cfg = load(env={}, config_path=tmp_path / "none.toml")
    assert cfg.idle_minutes == DEFAULT_IDLE_MINUTES == 20


def test_idle_minutes_comes_from_the_environment(tmp_path):
    cfg = load(env={"REMEM_IDLE_MINUTES": "5"},
               config_path=tmp_path / "none.toml")
    assert cfg.idle_minutes == 5


def test_a_zero_idle_window_falls_back_to_the_default(tmp_path):
    """Zero would make a session extractable the instant its first event
    lands - extraction racing a session still being worked in."""
    cfg = load(env={"REMEM_IDLE_MINUTES": "0"},
               config_path=tmp_path / "none.toml")
    assert cfg.idle_minutes == 20


def test_a_nonsense_idle_window_falls_back_to_the_default(tmp_path):
    cfg = load(env={"REMEM_IDLE_MINUTES": "soon"},
               config_path=tmp_path / "none.toml")
    assert cfg.idle_minutes == 20


def test_idle_minutes_can_come_from_the_config_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("idle_minutes = 45\n")
    assert load(env={}, config_path=path).idle_minutes == 45
