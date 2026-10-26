"""Database engine/session setup and helpers for the singleton JSON documents."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from jobbot.models import Base, KeyValue, utcnow
from jobbot.schemas import AppSettings, Profile


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(
            f"sqlite:///{self.path}", connect_args={"check_same_thread": False, "timeout": 30}
        )
        event.listen(self.engine, "connect", _sqlite_pragmas)
        self._sessionmaker = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """A session that commits on success and rolls back on error."""
        session = self._sessionmaker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def new_session(self) -> Session:
        return self._sessionmaker()


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    # WAL lets the web UI read while a background scan writes.
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


PROFILE_KEY = "profile"
SETTINGS_KEY = "settings"
RESUME_KEY = "resume"

M = TypeVar("M", bound=BaseModel)


def _load_doc(session: Session, key: str, model: type[M]) -> M:
    row = session.get(KeyValue, key)
    if row is None or not row.value:
        return model()
    try:
        return model.model_validate(row.value)
    except ValidationError:
        # Keep whatever still validates rather than failing the whole app on a bad document.
        data = {k: v for k, v in row.value.items() if k in model.model_fields}
        try:
            return model.model_validate(data)
        except ValidationError:
            return model()


def _save_doc(session: Session, key: str, doc: BaseModel | dict[str, Any]) -> None:
    value = doc.model_dump(mode="json") if isinstance(doc, BaseModel) else doc
    row = session.get(KeyValue, key)
    if row is None:
        session.add(KeyValue(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow()


def get_profile(session: Session) -> Profile:
    return _load_doc(session, PROFILE_KEY, Profile)


def save_profile(session: Session, profile: Profile) -> None:
    _save_doc(session, PROFILE_KEY, profile)


def get_settings(session: Session) -> AppSettings:
    return _load_doc(session, SETTINGS_KEY, AppSettings)


def save_settings(session: Session, settings: AppSettings) -> None:
    _save_doc(session, SETTINGS_KEY, settings)


def get_resume(session: Session) -> dict[str, Any]:
    """Resume metadata: {"filename", "path", "text", "uploaded_at"}; empty dict if none uploaded."""
    row = session.get(KeyValue, RESUME_KEY)
    return dict(row.value) if row and row.value else {}


def save_resume(session: Session, data: dict[str, Any]) -> None:
    _save_doc(session, RESUME_KEY, data)
