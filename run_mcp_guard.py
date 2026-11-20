"""Launcher that works without ``pip install``.

Python puts a script's own directory on ``sys.path``, so running this file by its
absolute path always finds the ``mcp_guard`` package next to it - no PYTHONPATH
needed, which keeps the environment of wrapped MCP servers untouched.
"""

from mcp_guard.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
