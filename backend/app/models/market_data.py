"""
Local market data cache. Stores 1m candles from Twelve Data.
Higher timeframes are aggregated on-the-fly from 1m data.
"""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Float, DateTime, BigInteger, Index, UniqueConstraint
from app.database import Base


class CandleCache(Base):
    __tablename__ = "candle_cache"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    symbol = Column(String(10), nullable=False)       # SPY, QQQ, IWM, DIA
    instrument = Column(String(10), nullable=False)    # ES, NQ, RTY, YM
    timestamp = Column(DateTime(timezone=True), nullable=False)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(BigInteger, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint('symbol', 'timestamp', name='uq_symbol_timestamp'),
        Index('ix_candle_instrument_ts', 'instrument', 'timestamp'),
        Index('ix_candle_symbol_ts', 'symbol', 'timestamp'),
    )
