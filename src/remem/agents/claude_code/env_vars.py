"""The Claude Code environment variables `remem config` may set.

This table lives on the adapter, not in services/, because
BASH_DEFAULT_TIMEOUT_MS is a fact about Claude Code rather than about remem.
Putting it in a service would make the service layer the place that knows
every agent's env schema - the exact coupling agents/registry.py exists to
prevent. A future adapter ships its own table and `remem config` works for it
with no service change. Only the *shape* of an entry is shared, and that
lives in agents/base.py so the service can validate without importing any
particular adapter.

Curated rather than passthrough: the help strings make `remem config list`
self-documenting, and two entries below encode traps that a passthrough
writer would let the user walk straight into.

Values and defaults transcribed from https://code.claude.com/docs/en/env-vars.
"""

from __future__ import annotations

from typing import Mapping

from remem.agents.base import EnvVar, Kind


def _var(name: str, kind: Kind, help: str, **kw) -> tuple[str, EnvVar]:
    return name, EnvVar(name=name, kind=kind, help=help, **kw)


CLAUDE_CODE_ENV_VARS: Mapping[str, EnvVar] = dict(
    [
        _var(
            "BASH_DEFAULT_TIMEOUT_MS",
            Kind.INT,
            "Default timeout for a bash command.",
            minimum=1,
            default="120000",
            duration=True,
        ),
        _var(
            "BASH_MAX_TIMEOUT_MS",
            Kind.INT,
            "Longest timeout the model may set on a bash command.",
            minimum=1,
            default="600000",
            duration=True,
        ),
        _var(
            "BASH_MAX_OUTPUT_LENGTH",
            Kind.INT,
            "Characters of bash output read back into context.",
            minimum=1,
            maximum=150000,
            default="30000",
        ),
        _var(
            "API_TIMEOUT_MS",
            Kind.INT,
            "Timeout for a single API request.",
            minimum=1,
            default="600000",
            duration=True,
        ),
        _var(
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
            Kind.INT,
            "Percentage of the context window that triggers auto-compaction.",
            minimum=1,
            maximum=100,
            note=(
                "Can only lower the built-in threshold, never raise it - a "
                "high value is accepted and does nothing."
            ),
        ),
        _var(
            "CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS",
            Kind.INT,
            "Stall timeout for a background subagent.",
            minimum=1,
            default="600000",
            duration=True,
        ),
        _var(
            "CLAUDE_AFK_TIMEOUT_MS",
            Kind.INT,
            "Auto-continue timeout for an unanswered question dialog.",
            minimum=0,
            duration=True,
        ),
        _var(
            "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR",
            Kind.BOOL,
            "Return to the project directory after every bash command.",
        ),
        # Presence-only. Any non-empty value enables the disabling, so "0"
        # would *disable telemetry* rather than re-enable it. Kind.PRESENCE is
        # what lets the service refuse "0" and point at `unset` instead.
        _var(
            "DISABLE_TELEMETRY",
            Kind.PRESENCE,
            "Disable telemetry collection.",
            note="Presence-only: unset it to re-enable, do not set it to 0.",
        ),
        _var(
            "DISABLE_ERROR_REPORTING",
            Kind.PRESENCE,
            "Disable error reporting.",
            note="Presence-only: unset it to re-enable, do not set it to 0.",
        ),
    ]
)

# Deliberately absent, and this comment is the reason they must stay absent:
#
#   ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_AWS_API_KEY,
#   ANTHROPIC_FOUNDRY_API_KEY, AWS_BEARER_TOKEN_BEDROCK and every other
#   credential - remem must never be the tool that writes a secret into a
#   JSON file on disk.
#
#   ANTHROPIC_BASE_URL and the whole *_BASE_URL family - repointing an agent
#   at a different inference endpoint is not a knowledge-store concern, and
#   getting it wrong silently sends prompts somewhere else.
#
#   CLAUDE_CONFIG_DIR - it names the file that would store it.
#
# tests/test_env_vars.py asserts each of these stays out.
