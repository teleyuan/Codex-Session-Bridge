#!/usr/bin/env python3
"""GUI entry point for the packaged Codex provider sync tool."""

from codex_provider_sync import main


if __name__ == "__main__":
    raise SystemExit(main(["--gui"]))
