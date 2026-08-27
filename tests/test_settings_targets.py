"""Which files `remem config` acts on, for a given agent.

That choice is policy - it decides where a write lands - so it lives in the
service. These tests use hand-written adapters rather than the registry
because the point is what the service does with an adapter's *capabilities*,
and claude-code is the only in-tree adapter that has them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

import pytest

from remem.agents.base import EnvVar, Kind
from remem.services.settings import list_settings, resolve_targets


TABLE: Mapping[str, EnvVar] = {
    "FAKE_TIMEOUT_MS": EnvVar("FAKE_TIMEOUT_MS", Kind.INT, "A fake.", minimum=1)
}


class FullAdapter:
    """An adapter with both optional capabilities, like claude-code."""

    name = "fake"

    def env_settings(self) -> Mapping[str, EnvVar]:
        return TABLE

    def settings_path(self, home: Path, env: Mapping[str, str]) -> Path:
        return home / ".fake" / "settings.json"


class TableOnlyAdapter:
    name = "table-only"

    def env_settings(self) -> Mapping[str, EnvVar]:
        return TABLE


class BareAdapter:
    """An adapter written before `remem config` existed."""

    name = "bare"


class BrokenAdapter:
    """An adapter whose capabilities raise.

    Not hypothetical: `settings_path` is where an adapter reads its own
    environment, so `Path(env["MY_CONFIG_DIR"])` with the variable unexported
    is the obvious way for a third-party adapter to fail.
    """

    name = "broken"

    def env_settings(self) -> Mapping[str, EnvVar]:
        return TABLE

    def settings_path(self, home: Path, env: Mapping[str, str]) -> Path:
        raise KeyError("MY_CONFIG_DIR")


class BrokenTableAdapter:
    name = "broken-table"

    def env_settings(self) -> Mapping[str, EnvVar]:
        raise RuntimeError("adapter is misconfigured")

    def settings_path(self, home: Path, env: Mapping[str, str]) -> Path:
        return home / ".broken" / "settings.json"


def test_the_agents_own_settings_path_is_used(tmp_path):
    # The failure this guards against: routing through a second adapter's
    # table and then writing the value into Claude Code's settings.json,
    # which is silent and corrupts another tool's config.
    targets = resolve_targets(FullAdapter(), tmp_path, {})
    assert targets.agent_path == tmp_path / ".fake" / "settings.json"
    assert "FAKE_TIMEOUT_MS" in targets.table


def test_an_adapter_without_env_settings_has_no_settable_variables(tmp_path):
    # The spec's documented outcome for an adapter that predates the
    # capability: reported as having no settable env vars, never a crash.
    targets = resolve_targets(BareAdapter(), tmp_path, {})
    assert targets.table == {}
    assert targets.agent_path is None


def test_a_table_without_a_settings_path_is_not_offered(tmp_path):
    # Knowing what exists is useless without knowing where to write it, and
    # offering the keys anyway would route a write at nothing. Same outcome
    # as having no table at all.
    targets = resolve_targets(TableOnlyAdapter(), tmp_path, {})
    assert targets.table == {}
    assert targets.agent_path is None


def test_a_capability_that_raises_degrades_instead_of_crashing(tmp_path):
    # The registry contract for this repo is that a broken third-party adapter
    # warns rather than breaking remem - see agents/registry.discover, which
    # catches a failed entry point load for the same reason. A probe that
    # raises has to land in the same place a missing probe does, or
    # `remem config list --agent broken` exits with a traceback.
    with pytest.warns(UserWarning, match="broken"):
        targets = resolve_targets(BrokenAdapter(), tmp_path, {})
    assert targets.table == {}
    assert targets.agent_path is None


def test_a_raising_env_settings_also_degrades(tmp_path):
    # Both probes, not just the one that happened to be found first.
    with pytest.warns(UserWarning, match="broken-table"):
        targets = resolve_targets(BrokenTableAdapter(), tmp_path, {})
    assert targets.table == {}
    assert targets.agent_path is None


def test_a_broken_adapter_degrades_loudly_rather_than_silently(tmp_path):
    # Silent degradation would leave the user's keys quietly missing from
    # `remem config list` with nothing to explain it. The adapter is named so
    # the warning points at what to fix.
    with pytest.warns(UserWarning, match="failed to report its settings"):
        resolve_targets(BrokenAdapter(), tmp_path, {})


def test_a_broken_adapter_still_leaves_remem_settings_usable(tmp_path):
    # Degrading must not cost the user the half that works: remem's own keys
    # do not come from the adapter at all.
    with pytest.warns(UserWarning):
        targets = resolve_targets(BrokenAdapter(), tmp_path, {})
    rows = list_settings(targets.remem_path, targets.agent_path, targets.table, {})
    assert any(row.key == "REMEM_MAX_CHARS" for row in rows)


def test_remem_config_honours_an_injected_remem_config_var(tmp_path):
    elsewhere = tmp_path / "somewhere" / "config.toml"
    targets = resolve_targets(
        BareAdapter(), tmp_path, {"REMEM_CONFIG": str(elsewhere)}
    )
    assert targets.remem_path == elsewhere


def test_listing_an_agent_with_no_settings_file_still_lists_remem(tmp_path):
    targets = resolve_targets(BareAdapter(), tmp_path, {})
    rows = list_settings(
        targets.remem_path, targets.agent_path, targets.table, {}
    )
    assert [r.key for r in rows]
    assert all(r.key.startswith("REMEM_") for r in rows)
