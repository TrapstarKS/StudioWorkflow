# Commands and recovery

Run in the game workspace, or use `studioworkflow --root /path/to/game COMMAND`. The root option goes before the command. All command output is JSON except interactive setup prompts and running service logs.

## Files and identity

```text
game-workspace/
├── studio-workflow.json          Place IDs and nonsecret settings
├── studio/<alias>/
│   ├── Files/                    Current editable scripts
│   └── Source/                   Last observed Studio sources; generated
└── .studio-workflow/             Private key, database, logs and history
```

Relative script paths come from `search` or the mirrored tree. They are file paths such as `ReplicatedStorage/Inventory.luau`, not dot-separated instance paths. Script kinds have different suffixes: `.luau`, `.local.luau`, `.legacy.luau` or a RunContext suffix. Unsafe name characters are percent-encoded; use the returned path unchanged.

`search` matches word prefixes across the path and source, with all query terms required. It searches registered current files in path order, capped at 50 results and 500 characters per excerpt. `--observed` uses the ranked Studio source index. Neither search mutates sync state. `--full` adds diagnostic information; use it when the compact state is insufficient.

`read` defaults to 60 lines and 16,000 source characters; maximums are 200 lines and 64,000 characters. `next_line` identifies remaining text. `partial_line` means the last line was cut by the character cap; increase the cap or read that line with local tools. The observed hash refers to Studio's indexed version, not the current local file.

## Command reference

| Command | Purpose |
| --- | --- |
| `init --place lobby=123 --agent both --startup` | Configure plugin/workspace and explicitly enable agent integration/login startup. |
| `run` | Supervise the bridge in the foreground; one supervisor per workspace. |
| `serve` | Run only the HTTP bridge, useful with your own process manager. |
| `stop` | Request supervisor shutdown; a login service may restart it. |
| `doctor` | Read-only configuration, index, plugin and authenticated service diagnostics. |
| `status [--place lobby]` | Full observed freshness, pending work and blocked-file details. |
| `search "term" [--place lobby] [--limit 5] [--observed]` | Search current Files or indexed Source. |
| `read --place lobby --path PATH [--start 1] [--lines 60]` | Read current Files with bounded output. |
| `changes --place lobby` | Compare editable files to observed Studio versions. |
| `checkout --place lobby --path PATH` | Hold the base before reading and editing. |
| `apply --place lobby --path PATH [--wait 20]` | Queue one compare-and-swap update; wait up to 55 seconds. |
| `operation ID` | Inspect the actual delivery outcome. |
| `rebase --place lobby --path PATH` | Prepare a three-way merge; a clean result stays held for review. |
| `release --place lobby --path PATH` | Release a hold without sending. Local changes are reconciled/preserved. |
| `resume --place lobby --path PATH` | Clear an investigated autosync block; normal identity/base checks still apply. |
| `pause-outbound [--place lobby] [--reason TEXT]` | Block outbound delivery; inbound observations continue. |
| `unpause-outbound [--place lobby]` | Remove the outbound pause. |
| `history --place lobby --path PATH` | List archived observed/edited versions for a script. |
| `export-version HASH --output /new/file.luau` | Export an archived version to a new file; refuses overwrite. |
| `checkpoint --place lobby` | Save the current observed inventory to a private local Git checkpoint. |
| `uninstall` | Remove owned plugin/startup and preserve workspace data. |

CLI exits nonzero for invalid commands/configuration, failed diagnostics, unresolved rebases and unconfirmed applies. An `apply` ending in `queued` or `delivered` is still pending; inspect its returned operation ID. `unchanged` is a successful no-op and releases the checkout.

## Resolve a conflict

1. Read `changes --place lobby` and inspect the affected local file plus observed `Source/`. A failed write leaves local work intact.
2. Run `rebase --place lobby --path PATH` while the observer is ready. A clean merge updates `Files/`, advances the base and holds the file. Inspect the result, then `apply`.
3. If `rebase` returns `conflict`, open its private conflict JSON. It contains base/local/Studio versions and may contain a proposal with markers. Resolve the source deliberately. Never send conflict markers as an automatic retry.

For overlapping edits, first prepare the resolved source in a separate file and retain the conflict record. Pause outbound, copy the latest observed `Source/PATH` into `Files/PATH`, and run `rebase` to establish that Studio version as the new base. Continue only if it returns a clean held result; if Studio changed again, inspect the new conflict. Replace the held `Files/PATH` with your reviewed resolution, unpause outbound, and apply. The normal checks still reject another concurrent change.

## Investigate an uncertain write

`delivered` means the observer received an operation; `applied` means an acknowledgment or matching source/identity confirmed it. A timeout after delivery can become `uncertain`. Look at `operation ID`, the observed source and the current session.

Do not clear database rows or retry the same intent blindly. Reconnect inventories can verify whether the intended source arrived or fence a previous epoch. Once the actual outcome is established, `rebase` or `resume` may be appropriate. Instance identity changes require inspection even if the path stayed the same.

## Restore an older version

```sh
studioworkflow history --place lobby --path ReplicatedStorage/Inventory.luau
studioworkflow export-version HASH --output /tmp/Inventory-recovered.luau
studioworkflow checkout --place lobby --path ReplicatedStorage/Inventory.luau
```

Compare the exported version with the current file. Copy the intended source into `Files/`, then apply it through the same base/identity checks. Export alone does not change Studio.

The service periodically archives local changes and checkpoints ready observed inventories. History is recovery material, not a complete game backup, and is not automatically pruned.
