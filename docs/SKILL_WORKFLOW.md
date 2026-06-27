# Skill Workflow

The Skill identifies user intent, calls MCP tools, and asks the current host
Agent to perform complex research and reasoning. It does not bypass evidence,
runtime state, or fact boundaries.

For chat, call `prepare_turn`, answer from the structured context, then call
`commit_turn`. For creation, submit evidence-backed artifacts and compile only
after schema validation succeeds.
