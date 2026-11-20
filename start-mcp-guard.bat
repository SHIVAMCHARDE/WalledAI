@echo off
REM Double-click to start MCP Guard in front of the Kiwi Flights MCP server.
REM Add the URL it prints in Claude > Settings > Connectors (works in Claude Web and Claude Desktop).
cd /d "%~dp0"
python run_mcp_guard.py start
pause
