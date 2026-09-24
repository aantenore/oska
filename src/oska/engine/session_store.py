"""Session state kept in a SQLite file; the implementation lives with the other stores."""

from oska.storage.sqlite_sessions import SessionStore

__all__ = ["SessionStore"]
