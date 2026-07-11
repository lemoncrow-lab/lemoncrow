"""Main reasoning service and HTTP API.

``create_app`` is available via ``lemoncrow.core.service.api.create_app`` —
it is NOT re-exported here so that importing lightweight submodules such
as ``config`` does not trigger the full FastAPI import chain (~500ms).
"""
