"""Event sources that publish onto the in-process :class:`EventBus`.

Each module here implements one source. Slice 0 ships
:class:`lithos_loom.sources.lithos_poller.LithosPoller`. Slice 1+ will add
``LithosSSE`` (live ``GET /events`` consumer) and ``FilesystemWatcher``.
"""
