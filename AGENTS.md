# StudioWorkflow development

This repository is a standalone local Roblox Studio source bridge. Keep game-specific code, place IDs, credentials, generated mirrors and unrelated skills out of distribution.

Use Python standard-library implementations for the core. Workspace data belongs to the configured workspace, not the installed package. Preserve conflict checks, session and epoch guards, history, and Edit-mode write barriers.

Before changing any Luau, read `.agents/skills/luau-best-practices/SKILL.md`. This is the plugin's own convention; do not import a game's coding rules.

Run the relevant unittest suite, including protocol regression tests. Runtime validation must use mocks or a separate disposable place, never publish or alter an existing game to test this tool.
