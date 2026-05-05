"""
database.py — SQLAlchemy engine, session factory, and FastAPI dependency.

Uses synchronous SQLAlchemy with SQLite. SQLite is single-writer and
file-locked, so async offers no real benefit here. For production at
scale, swap to PostgreSQL + async SQLAlchemy (asyncpg).

The `check_same_thread=False` flag is required because FastAPI handles
requests across multiple threads, but SQLite's default is to restrict
a connection to the thread that created it.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = "sqlite:///./factory_sense.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """
    FastAPI dependency — yields a scoped DB session per request.
    The session is guaranteed to close even if the endpoint raises.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
