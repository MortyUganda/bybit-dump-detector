"""
Runs Alembic migrations programmatically on startup.
Called by the `migrate` Docker service.
"""
import asyncio
from alembic.config import Config
from alembic import command
import os


def run_sync_migrations() -> None:
    alembic_cfg = Config(os.path.join(os.path.dirname(__file__), "alembic.ini"))
    command.upgrade(alembic_cfg, "head")


async def create_tables_direct() -> None:
    """
    Fallback: create all tables directly via SQLAlchemy (without Alembic).
    Useful for initial MVP setup before migrations are set up.
    """
    from app.db.models import Base
    from app.db.session import engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("All tables created successfully.")


if __name__ == "__main__":
    asyncio.run(create_tables_direct())
