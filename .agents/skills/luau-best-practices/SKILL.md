---
name: luau-best-practices
description: Maintain StudioWorkflow plugin and Luau protocol fixtures using this repository's standalone conventions.
---

Use descriptive camelCase locals and functions, PascalCase service names, and four-space indentation. Keep related operations together with blank lines between phases. Preserve the existing structure when making focused fixes.

Validate inputs at HTTP/protocol boundaries. Use pcall around Roblox APIs that may throw; propagate failures and retain pending work instead of silently dropping changes. Keep UpdateSourceAsync callbacks free of yielding operations and compare the observed source and identity before replacement.

Use explicit session, epoch and Edit-mode checks around yielding work. Disconnect event connections and close transport resources when a plugin unloads or is replaced. Avoid global state except the installation-scoped controller used to retire an earlier plugin instance.

Do not add game conventions, remote-event patterns or gameplay dependencies to this plugin. Tests should mock Roblox services and verify observable write barriers and protocol behavior. Never publish a place as part of development or setup.
