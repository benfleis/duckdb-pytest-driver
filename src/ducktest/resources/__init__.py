"""Shared, ready-made resources — ``service()`` / ``credential()`` descriptors a backend imports
instead of re-writing (see ``docs/SERVICES.md``).

Azurite (the Azure Blob Storage emulator) is the first instance; MinIO (an S3-compatible object store,
its S3 sibling) is the second. Each module here is *only values +
thin derivations* — the generic mechanism (attach, the block/derive contract, the ``alive`` probe hook,
the store lifecycle) lives in ``ducktest.plugin`` / ``ducktest.suites``. If a resource ever needs a
mechanism that isn't there, the mechanism goes in core first; the instance stays config.
"""
