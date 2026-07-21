"""MCP: external tool servers, joined through Seam 1."""
from forge.mcp.client import MCPClient, MCPServerSpec, RemoteTool
from forge.mcp.provider import MCPTool, MCPToolProvider

__all__ = ["MCPClient", "MCPServerSpec", "RemoteTool", "MCPTool", "MCPToolProvider"]
