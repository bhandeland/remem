# The one verification command for this project: `make check`.
#
# Stages short-circuit. make aborts on the first recipe that exits non-zero,
# so a lint failure never buries itself under a wall of type errors and test
# output - you read one failure, fix it, and run again. Order matters for
# the same reason: format rewrites, then lint fixes what it can, then the
# type checker and the suite see already-clean code.
#
# .NOTPARALLEL keeps that ordering even under `make -j`, and the @ prefixes
# keep the recipe lines themselves out of the output.
.PHONY: check format lint typecheck test
.NOTPARALLEL:

check: format lint typecheck test

format:
	@uv run ruff format -q src tests

lint:
	@uv run ruff check --fix -q src tests

typecheck:
	@uv run pyrefly check

test:
	@uv run pytest
