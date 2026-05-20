"""secondbrain.mcp — the MCP server plane.

This is the LLM-facing tool surface. Wraps the same `core/work_items` +
`core/wiki` helpers as the CLI, but exposes them as MCP tools so any
MCP-compatible LLM client (Claude Desktop, Cursor, Continue, etc.) can call them.

Run via:
    sb mcp                            # starts stdio MCP server
"""
