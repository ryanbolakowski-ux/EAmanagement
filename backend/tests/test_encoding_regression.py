"""Bug 10/12 — seeded strategy descriptions must not contain mojibake.

The classic artifact is UTF-8 read as Latin-1: an arrow (U+2192) becomes the
byte sequence that renders as 'â†'. Assert no description carries those bytes.
"""
import asyncio
from sqlalchemy import text
from app.database import async_session_factory, engine


def test_no_mojibake_in_strategy_descriptions():
    async def go():
        await engine.dispose()
        async with async_session_factory() as db:
            # \xc3\xa2 = 'â', \xe2\x80 = mojibake lead bytes for smart punctuation
            n = (await db.execute(text(
                "SELECT count(*) FROM strategies "
                "WHERE description LIKE '%' || chr(226) || chr(8224) || '%' "
                "   OR description LIKE '%' || chr(226) || chr(8364) || '%' "
                "   OR description ~ '\\u00c3\\u00a2'"
            ))).scalar()
        return n
    assert asyncio.run(go()) == 0
