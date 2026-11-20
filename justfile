set shell := ["powershell.exe", "-NoProfile", "-Command"]

default:
    @just --list

# The whole gate. Maya-free.
check: lock-check fmt-check lint types test

lock-check:
    uv lock --check

test:
    uv run --frozen --exact python -m pytest

lint:
    uv run --frozen --exact ruff check .

types:
    uv run --frozen --exact mypy

# Rewrites files; `check` uses fmt-check instead.
fmt:
    uv run --frozen --exact ruff format .

fmt-check:
    uv run --frozen --exact ruff format --check .

# Bring .venv in line with the committed lock. No Maya needed.
sync:
    uv sync --frozen --exact

# Once per clone: git runs the hooks from hooks/ of the working tree.
install-hooks:
    git config --local core.hooksPath hooks
