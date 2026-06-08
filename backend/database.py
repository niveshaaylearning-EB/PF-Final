import os
from sqlalchemy import create_engine, Column, Integer, String, Float, Text, text, event
from sqlalchemy.orm import declarative_base, sessionmaker

SQLALCHEMY_DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./portfolio.db")

# Pool settings for 30-50 concurrent users:
#   pool_size      — persistent connections kept open
#   max_overflow   — extra connections allowed on burst
#   pool_timeout   — wait up to 30s before raising "pool exhausted"
#   pool_pre_ping  — check connection health before handing it out
connect_args = {}
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args=connect_args,
    pool_size=20,
    max_overflow=30,
    pool_timeout=30,
    pool_pre_ping=True,
)

@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _record):
    """
    Applied once per physical connection:
    - WAL mode:  readers never block writers; writers never block readers.
                 Critical for concurrent access — without this SQLite uses
                 exclusive locks and every write blocks all reads.
    - synchronous=NORMAL: safe for WAL mode, 3-5× faster than FULL.
    - busy_timeout=10000: instead of instant "database is locked" errors,
                          wait up to 10 seconds for the lock to clear.
    - cache_size / mmap_size: keep hot pages in memory, reduce disk I/O.
    - temp_store=MEMORY: temp tables in RAM, not disk.
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=10000")
    cur.execute("PRAGMA cache_size=20000")
    cur.execute("PRAGMA mmap_size=268435456")   # 256 MB memory-mapped I/O
    cur.execute("PRAGMA temp_store=MEMORY")
    cur.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class Rationale(Base):
    __tablename__ = "rationales"
    id = Column(Integer, primary_key=True, index=True)
    stock_code = Column(String, unique=True, index=True)
    rationale_text = Column(Text)

class SimulationMod(Base):
    __tablename__ = "simulation_mods"
    id = Column(Integer, primary_key=True, index=True)
    basket_id = Column(String, index=True)
    stock_code = Column(String, index=True)
    override_type = Column(String) # 'add', 'modify', 'remove', 'delete'
    formula = Column(String, nullable=True)
    allocation = Column(Float, nullable=True)
    buy_price = Column(Float, nullable=True)
    cmp = Column(Float, nullable=True)

class SimulationSip(Base):
    __tablename__ = "simulation_sips"
    id = Column(Integer, primary_key=True, index=True)
    basket_id = Column(String, index=True)
    sip_date = Column(String)   # YYYY-MM-DD
    amount = Column(Float)


class NseStock(Base):
    __tablename__ = "nse_stocks"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, index=True)
    name = Column(String)

class BasketHistory(Base):
    __tablename__ = "basket_history"
    id = Column(Integer, primary_key=True, index=True)
    basket_id = Column(String, index=True)
    stock_code = Column(String, index=True)
    last_cmp = Column(Float)
    buy_price = Column(Float, nullable=True)       # buy price from the sheet
    allocation = Column(Float, nullable=True)      # allocation % from the sheet
    last_seen_date = Column(String)                # YYYY-MM-DD
    first_seen_date = Column(String, nullable=True)  # YYYY-MM-DD – set once when first observed
    stock_name = Column(String, nullable=True)     # human-readable name
    sector = Column(String, nullable=True)         # sector/theme

class SoldStock(Base):
    __tablename__ = "sold_stocks"
    id = Column(Integer, primary_key=True, index=True)
    basket_id = Column(String, index=True)
    stock_code = Column(String, index=True)
    buy_price = Column(Float)
    sell_price = Column(Float)
    sell_date = Column(String)
    buy_date = Column(String, nullable=True)       # first_seen_date at time of archiving
    sector = Column(String, nullable=True)         # sector/theme
    stock_name = Column(String, nullable=True)     # human-readable name
    weight = Column(Float, nullable=True)          # last known allocation % before removal

class HiddenStock(Base):
    """
    Stocks intentionally hidden from the holdings display via dashboard actions.
    hidden_reason = 'sold'    → user sold via dashboard; persists indefinitely
    hidden_reason = 'deleted' → user deleted via dashboard; expires after 7 days (expires_at)
    During sheet sync, any stock whose code is in this table is excluded from holdings.
    """
    __tablename__ = "hidden_stocks"
    id = Column(Integer, primary_key=True, index=True)
    basket_id = Column(String, index=True)
    stock_code = Column(String, index=True)
    hidden_reason = Column(String)              # 'sold' | 'deleted'
    stock_name = Column(String, nullable=True)
    buy_price = Column(Float, nullable=True)
    last_cmp = Column(Float, nullable=True)
    sector = Column(String, nullable=True)
    allocation = Column(Float, nullable=True)
    hidden_at = Column(String)                  # YYYY-MM-DD
    expires_at = Column(String, nullable=True)  # YYYY-MM-DD; NULL for 'sold'

class StockEvent(Base):
    """
    Audit log for every meaningful change to a holding:
    - 'added'              → stock first appeared (sheet or dashboard)
    - 'allocation_changed' → allocation % was updated
    - 'price_changed'      → buy price was updated
    - 'sold'               → stock sold via dashboard
    - 'deleted'            → stock soft-hidden via dashboard
    """
    __tablename__ = "stock_events"
    id = Column(Integer, primary_key=True, index=True)
    basket_id   = Column(String, index=True)
    stock_code  = Column(String, index=True)
    event_type  = Column(String)           # see docstring above
    description = Column(String)           # human-readable summary
    old_value   = Column(String, nullable=True)
    new_value   = Column(String, nullable=True)
    event_date  = Column(String)           # YYYY-MM-DD
    user_email  = Column(String, nullable=True)

class BasketNote(Base):
    __tablename__ = "basket_notes"
    id         = Column(Integer, primary_key=True, index=True)
    basket_id  = Column(String, unique=True, index=True)
    note_text  = Column(Text)
    updated_at = Column(String)   # YYYY-MM-DD HH:MM

class StockTarget(Base):
    __tablename__ = "stock_targets"
    id           = Column(Integer, primary_key=True, index=True)
    basket_id    = Column(String, index=True)
    stock_code   = Column(String, index=True)
    target_price = Column(Float, nullable=True)
    stoploss     = Column(Float, nullable=True)

class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    id            = Column(Integer, primary_key=True, index=True)
    basket_id     = Column(String, index=True)
    snapshot_name = Column(String)
    snapshot_date = Column(String)   # YYYY-MM-DD
    holdings_json = Column(Text)     # JSON-serialised holdings list + stats

class OtpCode(Base):
    __tablename__ = "otp_codes"
    id         = Column(Integer, primary_key=True, index=True)
    email      = Column(String, index=True)
    code       = Column(String)
    created_at = Column(String)   # ISO datetime
    used       = Column(Integer, default=0)  # 0=fresh 1=consumed

class LoginHistory(Base):
    __tablename__ = "login_history"
    id         = Column(Integer, primary_key=True, index=True)
    email      = Column(String, index=True)
    logged_at  = Column(String)
    ip_address = Column(String, nullable=True)
    location   = Column(String, nullable=True)

class AuditLog(Base):
    __tablename__ = "audit_log"
    id         = Column(Integer, primary_key=True, index=True)
    user_email = Column(String, index=True)
    event_type = Column(String)   # rebalance_upload | portfolio_change
    details    = Column(Text, nullable=True)
    created_at = Column(String, index=True)
    ip_address = Column(String, nullable=True)
    location   = Column(String, nullable=True)

class BenchmarkCache(Base):
    __tablename__ = "benchmark_cache"
    id         = Column(Integer, primary_key=True, index=True)
    symbol     = Column(String, index=True)
    period     = Column(String)    # 1M / 6M / 1Y / 3Y / 5Y
    net        = Column(Float, nullable=True)
    cagr       = Column(Float, nullable=True)
    fetched_at = Column(String)    # YYYY-MM-DD  (24 h TTL)

class BasketAnalyst(Base):
    __tablename__ = "basket_analyst"
    id           = Column(Integer, primary_key=True, index=True)
    basket_id    = Column(String, unique=True, index=True)
    analyst_name = Column(String)
    updated_by   = Column(String, nullable=True)
    updated_at   = Column(String, nullable=True)

class AllowedEmail(Base):
    __tablename__ = "allowed_emails"
    id       = Column(Integer, primary_key=True, index=True)
    email    = Column(String, unique=True, index=True)
    added_by = Column(String, nullable=True)
    added_at = Column(String)   # ISO datetime


class RebalanceAck(Base):
    __tablename__ = "rebalance_ack"
    id              = Column(Integer, primary_key=True, index=True)
    user_email      = Column(String, nullable=False, index=True)
    basket_id       = Column(String, nullable=False)
    rebalance_date  = Column(String, nullable=False)   # "DD Mon YYYY"
    acknowledged_at = Column(String, nullable=False)   # ISO datetime


Base.metadata.create_all(bind=engine)


def run_migrations():
    """Add new columns to existing tables if they don't already exist."""
    migrations = [
        "ALTER TABLE basket_history ADD COLUMN stock_name TEXT",
        "ALTER TABLE basket_history ADD COLUMN sector TEXT",
        "ALTER TABLE basket_history ADD COLUMN allocation REAL",
        "ALTER TABLE sold_stocks ADD COLUMN buy_date TEXT",
        "ALTER TABLE sold_stocks ADD COLUMN sector TEXT",
        "ALTER TABLE sold_stocks ADD COLUMN stock_name TEXT",
        # stock_events extra columns (table created by Base.metadata.create_all)
        "ALTER TABLE stock_events ADD COLUMN old_value TEXT",
        "ALTER TABLE stock_events ADD COLUMN new_value TEXT",
        # hidden_stocks is created by Base.metadata.create_all; extra columns listed for safety
        "ALTER TABLE hidden_stocks ADD COLUMN stock_name TEXT",
        "ALTER TABLE hidden_stocks ADD COLUMN buy_price REAL",
        "ALTER TABLE hidden_stocks ADD COLUMN last_cmp REAL",
        "ALTER TABLE hidden_stocks ADD COLUMN sector TEXT",
        "ALTER TABLE hidden_stocks ADD COLUMN allocation REAL",
        "ALTER TABLE hidden_stocks ADD COLUMN expires_at TEXT",
        "ALTER TABLE sold_stocks ADD COLUMN weight REAL",
        "ALTER TABLE stock_events ADD COLUMN user_email TEXT",
        "ALTER TABLE login_history ADD COLUMN location TEXT",
        "ALTER TABLE audit_log ADD COLUMN ip_address TEXT",
        "ALTER TABLE audit_log ADD COLUMN location TEXT",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                # Column already exists — safe to ignore
                pass


run_migrations()
