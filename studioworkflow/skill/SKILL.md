---
name: studioworkflow
description: Search and edit Roblox scripts through local files in a workspace configured with StudioWorkflow; diagnose or configure its local bridge.
---

StudioWorkflow synchronizes existing scripts automatically. Use local search and focused file reads for routine source work. It does not need Roblox MCP to read or update those sources.

## Find the workspace and command

Use the workspace's `studio-workflow.json` for place aliases. If `studioworkflow` is not available in the current environment, read `.studio-workflow/installation.json`: its `command` is an argv prefix and `module_directory` is the working directory for that invocation. Append the requested subcommand; keep its existing `--root` argument. Do not read or print `.studio-workflow/key` or the installed plugin source.

For a new installation, follow the repository README's installation section. Obtain the actual Place IDs from the user or existing project configuration. Do not substitute Universe IDs or infer them from unrelated projects. `init` configures a workspace and plugin; `--startup` and `--agent` explicitly select background startup and project instructions.

## Source work

- Start with `studioworkflow search "specific term" --place ALIAS --limit 5`. It searches current `Files`, including unsent edits, and returns compact Studio freshness. Use `--observed` for the last observed Studio source index, or `rg` for exact strings.
- Use `read --place ALIAS --path RELATIVE_PATH --start 1 --lines 60` for a bounded read of the current editable file. Output reports truncation and the next line. A partial line may need a larger `--max-chars` or a focused local read.
- Read the relevant current file under `studio/ALIAS/Files/` before editing. Follow the game's own Luau conventions. `Source/` and `.studio-workflow/` are generated; do not patch them as game code.
- For several saves, a long edit or concurrent work, run `checkout --place ALIAS --path RELATIVE_PATH` **before reading**. This holds the initial base and suspends automatic sending. Review the finished edit and run `apply` once. A failed apply keeps the hold; investigate its result.
- An ordinary save without checkout sends an existing, unambiguous script after debounce. Newly created local files do not create Studio instances, and deleting a local file does not delete an instance.
- Offline, Play, refreshing and conflicts block outbound writes. Resolve divergence with `rebase`, review the held merge, then `apply`. `resume` is for an investigated rejected/expired delivery; do not blindly retry uncertain writes or remove conflict state.
- Use `changes`, `operation`, `history` and `export-version` when needed. `doctor` or `status` is useful after startup, reconnect or an error; do not run a status loop after every save. Automatic synchronization runs outside the model.

Use Studio or appropriate tools for instance creation/deletion/rename, assets, hierarchy and runtime tests. StudioWorkflow neither publishes places nor runs gameplay tests. Report synchronization confirmation only when an operation result or observed source supports it. Do not promise a token-savings percentage without measurement.
