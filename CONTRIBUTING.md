# Contributing to Colink

Issues and small, focused pull requests are welcome. Use a self-contained sample
project; never attach real credentials, personal tunnel profiles or private code.

```sh
uv sync --locked
uv run ruff check src tests macos
uv run ruff format --check src tests macos
mkdir -p .artifacts
colink_test_dir=$(mktemp -d .artifacts/contributor-tests.XXXXXX)
uv run pytest -q --basetemp "$colink_test_dir"
uv run colink demo
uv run colink demo-local
```

Use a new independent test directory each time and preserve generated artifacts
until their owner approves cleanup. macOS desktop tests reject `/private` source
roots; do not disable that protection to run tests from `/tmp`.

Storage/protocol changes must test idempotent retry, conflicts, immutable reads,
two-state retention, disconnect and restart recovery. Tools must remain read-only;
ordinary answers must not reveal growing internal revision counters. Update
CHANGELOG and the relevant documentation, and label local versus real web proof.

Native build instructions are in docs/MACOS_APP.md. Do not commit dependencies,
caches, generated apps, runtime user data or personal ChatGPT plugin manifests.
Do not overwrite existing install/build output as part of a contribution.
