"""Which files `remem config` acts on, for a given agent.

That choice is policy - it decides where a write lands - so it lives in the
service. These tests use hand-written adapters rather than the registry
because the point is what the service does with an adapter's *capabilities*,
and claude-code is the only in-tree adapter that has them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

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
