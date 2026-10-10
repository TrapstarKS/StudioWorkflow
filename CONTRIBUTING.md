# Contributing

StudioWorkflow is a standalone source bridge. Keep game IDs, credentials, generated mirrors and unrelated skills out of the package.

## Architecture

- `studioworkflow/bridge.py`: SQLite state, current local search, observed index, history, conflict checks and authenticated HTTP protocol.
- `observer.luau`: Studio observer and editor-aware compare-and-swap writes. Configuration is injected into a local XML plugin at installation.
- `installation.py`: validated workspace configuration, owned plugin/agent files and opt-in OS startup.
- `supervisor.py`: workspace lock, process lifecycle and bounded logs.
- `__main__.py`, `config.py`, `diagnostics.py`: CLI, configuration validation and read-only diagnostics.
- `skill/SKILL.md`: the standalone user workflow. It delegates Luau conventions to the target game.

The core uses the Python standard library. Build tools are packaging dependencies, not runtime dependencies. Keep the authentication key outside the configuration and do not print embedded plugin source in diagnostics.

## Tests

```sh
python -m pip install .
python -m unittest discover -s tests -v
```

Install [Lune](https://github.com/lune-org/lune) to run the optional Luau fixtures. Without it, those tests explicitly skip. Python integration tests open an ephemeral loopback server and simulate observers; OS startup tests use temporary folders and mocks. Do not install test plugins into a real user's Studio folder.

Before changing Luau, read the repository's [plugin conventions](.agents/skills/luau-best-practices/SKILL.md). Preserve session/epoch fences around yielding operations and exact base/identity checks inside `UpdateSourceAsync`.

Regression coverage includes concurrent edits, acknowledgment loss, stale sessions, Play transitions, chunk transport, deleted/renamed scripts, portable paths, private history, bounded search/read output, installation rollback, ownership and supervisor restarts.

## Release checks

Build a wheel, install it into a fresh environment, verify the packaged plugin/skill resources and exercise setup with an isolated workspace/plugin directory. Run the suite on macOS and Windows. Use a disposable place for a real Studio smoke test and document what was actually tested; mocked services do not prove destination permissions or Team Create behavior.

Share only clean source archives or wheels. Workspace keys, generated plugins, indexes, mirrors and logs must never enter releases. Report a bug with OS/Python versions, the command, sanitized diagnostics and reproduction steps; omit source or credentials unless intentionally sharing them.
