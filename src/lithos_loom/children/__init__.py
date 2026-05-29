"""Subprocess child entry points spawned by the Supervisor.

Each module in this package is invokable via ``python -m
lithos_loom.children.<name> --config <path>`` and runs until SIGTERM, owning
its own asyncio event loop and (typically) its own in-process EventBus.
"""
