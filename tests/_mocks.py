"""Mocks shared across the test modules.

Extracted when tests were split by subject: several modules need the
same IndexManager and AccountMap doubles, and duplicating them would
let them drift apart.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


def mock_index(location=None, has_index=True):
    """An IndexManager mock that resolves every id to `location`."""
    mgr = MagicMock()
    mgr.has_index.return_value = has_index
    mgr.find_email_location.return_value = location
    return mgr


def mock_acct_map(uuid_to_name="Work", excluded_uuids=None):
    m = MagicMock()
    m.ensure_loaded = AsyncMock()
    m.names_to_uuids.return_value = set(excluded_uuids or [])
    m.name_to_uuid.return_value = None
    m.uuid_to_name.return_value = uuid_to_name
    return m
