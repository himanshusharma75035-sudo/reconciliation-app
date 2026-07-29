import enum
import datetime
import uuid
import os

from dotenv import load_dotenv
from sqlalchemy import (
    create_engine, Column, String, Float, DateTime, Boolean,
    Integer, Text, ForeignKey, Index, UniqueConstraint, Numeric, LargeBinary
)
from sqlalchemy.dialects.mysql import LONGBLOB

# Recycle-bin payloads are gzipped JSON and routinely exceed MySQL's 64 KB BLOB cap,
# so use LONGBLOB there while staying plain BLOB on SQLite (dual-DB by design).
BIGBLOB = LargeBinary().with_variant(LONGBLOB, "mysql")
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship

# ── Money type ────────────────────────────────────────────────────────────────
# DECIMAL(15,2) on MySQL/Postgres (exact storage for ₹ amounts); asdecimal=False
# returns plain floats to Python so existing engine arithmetic is unchanged.
# SQLite stores numerics dynamically, so dev behaviour is identical.
MONEY = Numeric(15, 2, asdecimal=False)

# ── Load .env from backend/ directory ────────────────────────────────────────
_env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
load_dotenv(_env_path)

# ── Database connection ───────────────────────────────────────────────────────
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./recon.db"          # fallback if .env is missing
)

_is_sqlite = "sqlite" in DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    # MySQL connection pool settings
    pool_pre_ping=True,             # test connection before use (handles dropped connections)
    pool_size=10,                   # keep 10 connections open
    max_overflow=20,                # allow up to 20 extra connections under load
    pool_recycle=1800,              # recycle connections every 30 min (avoids MySQL wait_timeout)
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def generate_id():
    return str(uuid.uuid4())


# ─── Enums ────────────────────────────────────────────────────────────────────
class UserRole(str, enum.Enum):
    admin = "admin"
    user  = "user"

class UploadStatus(str, enum.Enum):
    pending    = "pending"
    processing = "processing"
    done       = "done"
    failed     = "failed"

class ReconStatus(str, enum.Enum):
    matched           = "matched"
    unmatched         = "unmatched"
    manual_matched    = "manual_matched"
    src_assigned      = "src_assigned"
    fee_matched       = "fee_matched"         # bank fee/charge — auto-closed, not reconciled
    fund_transfer     = "fund_transfer"       # bank fund transfer — initially auto-closed; can become interbank_matched
    amount_mismatch   = "amount_mismatch"     # IDs matched but amounts differ — needs review
    duplicate         = "duplicate"           # excess copy — same TID exists on the other side already matched
    failed            = "failed"              # internal dump row whose status = FAILED/FAILURE/etc.
    reversal_matched  = "reversal_matched"    # bank-side reversal paired with its original (bank↔bank)
    internal_matched  = "internal_matched"    # internal-to-internal pair (Success + Refunded same TID)
    # ── New statuses ───────────────────────────────────────────────────────────
    interbank_matched = "interbank_matched"   # fund_transfer CR ↔ fund_transfer DR across two bank accounts (matched via UTR)
    adhoc_settlement  = "adhoc_settlement"    # bank-side debit given to CSP/SCSP externally (no Simplibank record); human-tagged with mandatory remark
    human_override    = "human_override"      # status manually changed by a human user (with mandatory remark + audit trail)

class SideCode(str, enum.Enum):
    bank     = "bank"
    internal = "internal"

class Partner(str, enum.Enum):
    airtel = "airtel"
    fino   = "fino"
    other  = "other"


# ─── Models ───────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id              = Column(String(36),  primary_key=True, default=generate_id)
    username        = Column(String(100), unique=True, nullable=False)
    email           = Column(String(200), unique=True, nullable=True)
    hashed_password = Column(String(255), nullable=False)
    role            = Column(String(50),  default=UserRole.user)
    full_name       = Column(String(200), nullable=True)
    is_active       = Column(Boolean,     default=True)
    permissions     = Column(Text, default='{"upload":true,"run_recon":true,"src_assign":true,"reports":true,"logic_builder":false,"override":false,"manual_match":true,"clear_data":false,"approver":false}')
    # JSON list of product ids this user may reconcile (e.g. ["dmt","bbps"]).
    # Empty list / null = access to ALL products. Admins always see everything.
    allowed_products = Column(Text, default="[]")
    created_at      = Column(DateTime,    default=datetime.datetime.utcnow)


class APIKey(Base):
    """
    Long-lived API keys for programmatic access (agents, external integrations).
    Key is stored as a SHA-256 hash — the plaintext is only shown once at creation.
    Permissions mirror the User permissions JSON so keys can be scoped.
    """
    __tablename__ = "api_keys"

    id           = Column(String(36),  primary_key=True, default=generate_id)
    name         = Column(String(200), nullable=False)          # e.g. "OpenClaw Agent"
    description  = Column(Text, nullable=True)
    key_hash     = Column(String(64), unique=True, nullable=False)  # SHA-256 hex
    key_prefix   = Column(String(10), nullable=False)           # first 8 chars for identification
    permissions  = Column(Text, default='{"upload":true,"run_recon":true,"src_assign":false,"reports":true,"logic_builder":false}')
    is_active    = Column(Boolean, default=True)
    created_by   = Column(String(36), ForeignKey("users.id"))
    last_used_at = Column(DateTime, nullable=True)
    created_at   = Column(DateTime, default=datetime.datetime.utcnow)
    expires_at   = Column(DateTime, nullable=True)              # None = never expires
    # Comma-separated allowlist of source IPs/CIDRs (e.g. "10.0.0.5,203.0.113.0/24").
    # Empty/None = allowed from anywhere (back-compat). Enforced in get_current_user.
    allowed_ips  = Column(String(500), nullable=True)


class ModuleUploadHash(Base):
    """
    SHA-256 of every file ingested by the dedicated modules (E-Value, BBPS, SBI…)
    so the exact same file cannot be ingested twice by accident. The core engine
    already blocks re-uploads via UploadSession; this covers the module paths.
    """
    __tablename__ = "module_upload_hashes"

    id          = Column(String(36), primary_key=True, default=generate_id)
    module      = Column(String(20), index=True)     # evalue | bbps | sbi
    side        = Column(String(30))                 # internal | bank | …
    sha256      = Column(String(64), index=True)
    filename    = Column(String(300))
    uploaded_by = Column(String(100), nullable=True)
    uploaded_at = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (UniqueConstraint("module", "sha256", name="uq_module_file_hash"),)


class ApprovalRequest(Base):
    """
    Maker-checker queue (Tier 1). When maker_checker is enabled, manual matches /
    overrides by non-admin users are stored here as pending and only executed when
    a DIFFERENT user approves them.
    """
    __tablename__ = "approval_requests"

    id            = Column(String(36),  primary_key=True, default=generate_id)
    action_type   = Column(String(30),  index=True)   # override | bulk_override | manual_match | src_assign
    payload       = Column(Text,        default="{}") # JSON args needed to execute the action
    summary       = Column(String(400))               # human-readable one-liner for the queue
    partner       = Column(String(50),  nullable=True)
    requested_by  = Column(String(100), index=True)
    requested_at  = Column(DateTime,    default=datetime.datetime.utcnow)
    status        = Column(String(15),  default="pending", index=True)  # pending | approved | rejected
    reviewed_by   = Column(String(100), nullable=True)
    reviewed_at   = Column(DateTime,    nullable=True)
    review_note   = Column(String(400), nullable=True)


class FeeRule(Base):
    """
    Expected fee / commission per partner (Tier 1 fee recon). Used by
    /recon/verify-fees to flag rows where bank net amount ≠ amount − expected fee.
    """
    __tablename__ = "fee_rules"

    id           = Column(String(36), primary_key=True, default=generate_id)
    partner      = Column(String(50), index=True)       # pg / qr / aeps / …
    label        = Column(String(200))                  # e.g. "PayU MDR 1.1% + GST"
    fee_type     = Column(String(10), default="percent")  # percent | flat
    fee_value    = Column(MONEY, default=0)             # 1.1 (%) or ₹ flat
    gst_percent  = Column(MONEY, default=18.0)          # GST applied on the fee
    tolerance    = Column(MONEY, default=0.5)           # ₹ tolerance when comparing
    is_active    = Column(Boolean, default=True)
    created_at   = Column(DateTime, default=datetime.datetime.utcnow)


class RuleSuggestion(Base):
    """
    Tier 2 rule learning: every manual match records which fields actually
    matched. When the same (partner, field-set) pattern repeats, it surfaces in
    Workflow → Rule Suggestions for one-click promotion to a real MatchRule.
    """
    __tablename__ = "rule_suggestions"

    id          = Column(String(36),  primary_key=True, default=generate_id)
    partner     = Column(String(50),  index=True)
    fields_csv  = Column(String(200))                 # e.g. "utr_number,amount"
    hit_count   = Column(Integer,     default=1)
    status      = Column(String(15),  default="suggested", index=True)  # suggested | accepted | dismissed
    last_seen   = Column(DateTime,    default=datetime.datetime.utcnow)
    created_at  = Column(DateTime,    default=datetime.datetime.utcnow)

    __table_args__ = (UniqueConstraint("partner", "fields_csv", name="uq_rule_suggestion"),)


class SystemSetting(Base):
    """Simple key/value app settings (e.g. maker_checker_enabled)."""
    __tablename__ = "system_settings"

    key        = Column(String(50), primary_key=True)
    value      = Column(String(500), default="")
    updated_by = Column(String(100), nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class SrcCode(Base):
    """Managed catalog of SRC (source / disposition) reason codes. Replaces the
    hard-coded SRC_CODES list so codes can be added/deactivated from Configuration
    without a redeploy. Seeded with the original 9 codes; served (active only) at
    GET /api/recon/src-codes and validated against by every product's assign-src.
    Codes are deactivated, never hard-deleted, so already-tagged rows stay valid."""
    __tablename__ = "src_codes"

    id         = Column(String(36),  primary_key=True, default=generate_id)
    code       = Column(String(40),  unique=True, nullable=False, index=True)  # canonical UPPER_SNAKE
    label      = Column(String(200), nullable=True)                            # human description
    is_active  = Column(Boolean,     default=True, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime,    default=datetime.datetime.utcnow)


class UploadSession(Base):
    __tablename__ = "upload_sessions"

    id                = Column(String(36),  primary_key=True, default=generate_id)
    user_id           = Column(String(36),  ForeignKey("users.id"))
    partner           = Column(String(50),  nullable=False)   # airtel / fino / mixed
    side              = Column(String(20),  nullable=False)   # bank / internal
    original_filename = Column(String(500))
    stored_filename   = Column(String(500))
    upload_date       = Column(DateTime,    default=datetime.datetime.utcnow)
    recon_date        = Column(String(20))                    # YYYY-MM-DD
    status            = Column(String(20),  default=UploadStatus.pending)
    row_count         = Column(Integer,     default=0)
    column_mapping    = Column(Text)                          # JSON: {std_col: file_col}
    error_message     = Column(Text,        nullable=True)

    transactions = relationship("Transaction", back_populates="upload_session")


class Transaction(Base):
    __tablename__ = "transactions"

    id                = Column(String(36),  primary_key=True, default=generate_id)
    upload_session_id = Column(String(36),  ForeignKey("upload_sessions.id"))
    partner           = Column(String(50),  nullable=True)
    side              = Column(String(20),  nullable=True)
    recon_date        = Column(String(20),  nullable=True)    # YYYY-MM-DD

    # ── Core match fields ─────────────────────────────────────────────────────
    # NO unique constraint — duplicates are intentionally preserved (bank may
    # have two rows for the same TID if a retry happened; recon engine handles it)
    eko_tid          = Column(String(100), nullable=True)
    tracking_number  = Column(String(100), nullable=True)
    utr_number       = Column(String(100), nullable=True)

    # ── Financial fields ──────────────────────────────────────────────────────
    amount           = Column(MONEY,       nullable=True)
    net_amount       = Column(MONEY,       nullable=True)     # PG only: PayU net after fees
    dr_cr            = Column(String(5),   nullable=True)     # "DR" or "CR"
    status           = Column(String(100), nullable=True)
    transaction_date = Column(String(50),  nullable=True)

    # ── Row classification ────────────────────────────────────────────────────
    # "txn"               — normal DMT transaction (participates in recon)
    # "fee_charge"        — bank fee/service charge  (auto fee_matched, skipped in recon)
    # "settlement_credit" — RTGS/NEFT inward credit  (auto fund_transfer, skipped in recon)
    # "fund_transfer"     — bank fund transfer out    (auto fund_transfer, skipped in recon)
    row_type         = Column(String(30),  nullable=False, default="txn")

    # ── Audit / raw storage ───────────────────────────────────────────────────
    raw_data         = Column(Text)                           # full original row as JSON
    # Original free-text bank-statement narration/description, BANK SIDE ONLY
    # (NULL for internal-dump rows). Read-only display field — never used in
    # matching. Populated on ingest from the mapped description column and
    # backfilled from raw_data for pre-existing rows.
    bank_description = Column(Text, nullable=True)
    # CSP (retailer) identity carried from the internal dump, INTERNAL SIDE ONLY
    # (NULL for bank-statement rows). Read-only display/search fields — never used
    # in matching. Populated on ingest from the dump's CSPCode / MerchantName
    # columns and backfilled from raw_data for pre-existing rows.
    csp_code         = Column(String(40),  nullable=True)
    csp_name         = Column(String(255), nullable=True)

    # ── Recon state ───────────────────────────────────────────────────────────
    recon_status     = Column(String(30),  default=ReconStatus.unmatched)
    matched_with_id  = Column(String(36),  nullable=True)     # FK to the matched Transaction
    # match_id: shared identifier on BOTH sides of a matched pair.
    # Format: RCN-{PARTNER}-{YYYYMMDD}-{6HEX}  e.g. RCN-FINO-20260415-A3F9B2
    # Use this to look up "which two transactions were matched together".
    match_id         = Column(String(40),  nullable=True, index=True)
    src_code         = Column(String(50),  nullable=True)
    src_note         = Column(String(500), nullable=True)
    # Human override tracking — populated whenever a human changes status/matching
    override_note    = Column(String(1000), nullable=True)  # mandatory remark (rejected if empty)
    override_by      = Column(String(100),  nullable=True)  # username who overrode
    override_at      = Column(DateTime,     nullable=True)  # timestamp of override
    prev_recon_status= Column(String(50),   nullable=True)  # status before override (full trail)

    # ── Exception workflow (Tier 1) ──
    assigned_to      = Column(String(100),  nullable=True, index=True)  # username owning this open item
    assigned_at      = Column(DateTime,     nullable=True)
    exception_reason = Column(String(40),   nullable=True)  # reason code (BANK_DELAY, MISSING_UTR, …)

    # Source bank account number (bank side). A partner (e.g. Axis) may have several
    # accounts; recon still runs under one partner but each bank row is tagged so it
    # can be filtered/reported per account.
    bank_account     = Column(String(40),   nullable=True, index=True)

    # Running balance from the bank statement (bank side only; NULL when the format
    # has no balance column). Display/reporting ONLY — never used in matching.
    balance          = Column(MONEY,        nullable=True)

    upload_session   = relationship("UploadSession", back_populates="transactions")

    # ── Indexes ───────────────────────────────────────────────────────────────
    __table_args__ = (
        # 1. Core match fields — used in every reconciliation lookup
        Index("ix_txn_eko_tid",         "eko_tid"),
        Index("ix_txn_tracking_number", "tracking_number"),
        Index("ix_txn_utr_number",      "utr_number"),

        # 2. Composite: (partner + recon_date + side) — covers Dashboard queries
        #    "give me all rows for fino / 2026-04-15 / bank"
        Index("ix_txn_partner_date_side", "partner", "recon_date", "side"),

        # 3. Composite: (partner + recon_date + recon_status) — covers Open Items queries
        #    "give me all unmatched rows for fino / 2026-04-15"
        Index("ix_txn_partner_date_status", "partner", "recon_date", "recon_status"),

        # 4. Composite: (recon_status + row_type) — covers matching engine inner loop
        #    "give me all unmatched txn rows"
        Index("ix_txn_status_type", "recon_status", "row_type"),

        # 5. matched_with_id — used when resolving match pairs
        Index("ix_txn_matched_with", "matched_with_id"),
    )


class ReconRun(Base):
    __tablename__ = "recon_runs"

    id                 = Column(String(36),  primary_key=True, default=generate_id)
    user_id            = Column(String(36),  ForeignKey("users.id"))
    partner            = Column(String(50))
    recon_date         = Column(String(20))
    run_at             = Column(DateTime,    default=datetime.datetime.utcnow)
    total_bank         = Column(Integer,     default=0)
    total_internal     = Column(Integer,     default=0)
    matched            = Column(Integer,     default=0)
    unmatched_bank     = Column(Integer,     default=0)
    unmatched_internal = Column(Integer,     default=0)
    rules_applied      = Column(Text)                         # JSON list of rule names
    notes              = Column(Text,        nullable=True)


class MatchRule(Base):
    __tablename__ = "match_rules"

    id           = Column(String(36),  primary_key=True, default=generate_id)
    name         = Column(String(200), nullable=False)
    partner      = Column(String(50))                         # airtel / fino / all
    priority     = Column(Integer,     default=1)
    match_fields = Column(Text)                               # JSON list: ["eko_tid","tracking_number"]
    is_active    = Column(Boolean,     default=True)
    created_by   = Column(String(36),  ForeignKey("users.id"))
    created_at   = Column(DateTime,    default=datetime.datetime.utcnow)
    description  = Column(Text,        nullable=True)
    # Which two sides this rule pairs. Default preserves the only historical behaviour
    # (bank statement ↔ Simplibank internal dump). Same-side scopes pair OPPOSITE
    # DR/CR net-zero rows (a reversal/contra) and write reversal_matched / internal_matched:
    #   'bank_internal'     — bank ↔ Simplibank  (the normal recon; every legacy rule)
    #   'bank_bank'         — bank ↔ bank         (a payout debit and its reversal credit)
    #   'internal_internal' — Simplibank ↔ Simplibank (a dump debit and its reversing credit)
    scope        = Column(String(30),  default="bank_internal")


class ColumnMappingTemplate(Base):
    __tablename__ = "column_mapping_templates"

    id         = Column(String(36),  primary_key=True, default=generate_id)
    name       = Column(String(200), nullable=False)
    partner    = Column(String(50))
    side       = Column(String(20))
    mapping    = Column(Text)                                 # JSON
    created_by = Column(String(36),  ForeignKey("users.id"))
    created_at = Column(DateTime,    default=datetime.datetime.utcnow)


class BankBalanceSnapshot(Base):
    """
    Funds-position feature: ONE row per (product, account, statement_date) —
    the day's opening/closing balance + DR/CR movement as stated by the uploaded
    bank data. Written at ingest time (statement row order is only known then;
    row IDs are UUIDs and not sortable). Re-uploads UPDATE the same key, never
    duplicate. Read by the EOD Funds Position dashboard/report. Reporting only —
    reconciliation never touches this table.
    """
    __tablename__ = "bank_balance_snapshots"

    id              = Column(String(36),  primary_key=True, default=generate_id)
    product         = Column(String(30),  nullable=False)   # dmt | aeps | pg | ... | evalue | kiosk
    partner         = Column(String(50),  nullable=False)   # partner slug / module label
    bank_account    = Column(String(60),  nullable=True)    # account no/label ('' = partner-level)
    statement_date  = Column(String(10),  nullable=False)   # YYYY-MM-DD (as per the data — T+1 aware)
    opening_balance = Column(MONEY,       nullable=True)    # explicit line, or derived closing-(cr-dr)
    closing_balance = Column(MONEY,       nullable=True)
    total_dr        = Column(MONEY,       default=0.0)
    total_cr        = Column(MONEY,       default=0.0)
    txn_count       = Column(Integer,     default=0)
    source          = Column(String(20),  default="running")  # running | stated | derived
    uploaded_by     = Column(String(100), nullable=True)
    created_at      = Column(DateTime,    default=datetime.datetime.utcnow)
    updated_at      = Column(DateTime,    default=datetime.datetime.utcnow,
                             onupdate=datetime.datetime.utcnow)

    __table_args__ = (
        Index("ix_bbs_key", "product", "partner", "bank_account", "statement_date", unique=True),
        Index("ix_bbs_date", "statement_date"),
    )


class AuditLog(Base):
    """
    Immutable activity log. Written on every significant action.
    Never update or delete rows — append-only.
    """
    __tablename__ = "audit_logs"

    id          = Column(String(36),  primary_key=True, default=generate_id)
    user_id     = Column(String(36),  ForeignKey("users.id"), nullable=True)
    username    = Column(String(100), nullable=True)    # denormalized — survives user deletion
    action      = Column(String(80),  nullable=False)   # upload | run_recon | manual_match |
                                                        # assign_src | bulk_src | delete | clear |
                                                        # human_override | tag_adhoc | interbank_match
    # action_type distinguishes automated app actions from deliberate human decisions.
    # "app"   = system did this automatically (matching engine, auto-recon, upload, etc.)
    # "human" = a logged-in user explicitly triggered this action
    # Filter audit log by action_type="human" to see only human decisions.
    action_type = Column(String(10),  default="app")    # "app" | "human"
    entity_type = Column(String(50),  nullable=True)    # transaction | upload_session | recon_run
    entity_id   = Column(String(36),  nullable=True)
    detail      = Column(Text,        nullable=True)    # JSON blob — search-friendly context
    # previous_state captures the before-snapshot for any mutating human action,
    # enabling full before/after traceability without querying historical snapshots.
    previous_state = Column(Text,     nullable=True)    # JSON: {recon_status, match_id, ...} before change
    created_at  = Column(DateTime,    default=datetime.datetime.utcnow)

    __table_args__ = (
        Index("ix_audit_user",   "user_id"),
        Index("ix_audit_action", "action"),
        Index("ix_audit_ts",     "created_at"),
    )


class UploadHistory(Base):
    """
    One row per confirmed file upload.
    Tracks who uploaded what, when, how many rows, and which format was detected.
    """
    __tablename__ = "upload_history"

    id              = Column(String(36),  primary_key=True, default=generate_id)
    user_id         = Column(String(36),  ForeignKey("users.id"), nullable=True)
    username        = Column(String(100), nullable=True)
    filename        = Column(String(500), nullable=True)
    partner         = Column(String(50),  nullable=True)
    side            = Column(String(20),  nullable=True)
    recon_date      = Column(String(20),  nullable=True)    # YYYY-MM-DD or "auto (multi-date)"
    rows_inserted   = Column(Integer,     default=0)
    format_detected = Column(String(200), nullable=True)
    uploaded_at     = Column(DateTime,    default=datetime.datetime.utcnow)

    __table_args__ = (
        Index("ix_ulhist_partner_date", "partner", "recon_date"),
        Index("ix_ulhist_ts",           "uploaded_at"),
    )


class IngestionEvent(Base):
    """
    Append-only ingestion lineage ledger (roadmap 1.4 — additive).

    One row per ingestion attempt across ALL channels (interactive upload and
    watch-folder/auto-upload), capturing the file fingerprint, detected preset,
    row accounting (read / accepted / skipped), WLR/FREC outcome, duration and
    the resulting upload_session — so ingestion stops being a silent `skipped`
    counter and failures on the least-supervised channels become visible.

    Written by core.ingestion_ledger.record_ingestion_event() in its OWN
    transaction so a logging failure can never block or roll back an ingest.
    Never updated or deleted — append-only, like AuditLog. `upload_session_id`
    is intentionally NOT a ForeignKey (additive rule: no cascade onto live
    tables).
    """
    __tablename__ = "ingestion_events"

    id                = Column(String(36),  primary_key=True, default=generate_id)
    created_at        = Column(DateTime,    default=datetime.datetime.utcnow)
    channel           = Column(String(20),  nullable=True)   # upload | watch_folder | auto | api
    status            = Column(String(20),  nullable=True)   # completed | failed | blocked
    partner           = Column(String(50),  nullable=True)
    side              = Column(String(20),  nullable=True)
    recon_date        = Column(String(20),  nullable=True)
    username          = Column(String(100), nullable=True)
    # File lineage
    filename          = Column(String(500), nullable=True)
    file_sha256       = Column(String(64),  nullable=True)
    file_size         = Column(Integer,     nullable=True)
    preset_detected   = Column(String(120), nullable=True)
    # Row accounting (counters the ingest pipeline already computes)
    rows_read         = Column(Integer,     nullable=True)
    rows_accepted     = Column(Integer,     nullable=True)
    rows_skipped      = Column(Integer,     nullable=True)
    skip_breakdown    = Column(Text,        nullable=True)   # JSON {reason: count}; null until skip points instrumented
    # Controls / outcome
    wlr_frec          = Column(String(20),  nullable=True)   # passed | not_checked | n/a
    duration_ms       = Column(Integer,     nullable=True)
    upload_session_id = Column(String(36),  nullable=True)   # deliberately NOT a FK
    detail            = Column(Text,        nullable=True)   # JSON blob — extra context / error message
    # roadmap 1.6: read-only per-file data-quality profile (JSON) — blank/amount/
    # date parse rates, duplicate-key rate, non-blocking warnings. Display-only.
    dq_profile        = Column(Text,        nullable=True)

    __table_args__ = (
        Index("ix_ingevt_created",         "created_at"),
        Index("ix_ingevt_channel_status",  "channel", "status"),
        Index("ix_ingevt_partner",         "partner"),
    )


class SavedView(Base):
    """
    Per-user saved filter set for a list screen (roadmap 1.3 — additive).

    A saved view is just a stored query (the filter dict) replayed through the
    UNCHANGED list endpoint — it never alters Open-Items query semantics, buckets,
    or vocabulary (behavior-contract #14). `user_id` is the owner; `is_shared`
    exposes a view read-only to other users for the same page. Not a FK (no
    cascade onto users).
    """
    __tablename__ = "saved_views"

    id         = Column(String(36),  primary_key=True, default=generate_id)
    user_id    = Column(String(36),  nullable=True, index=True)   # owner
    username   = Column(String(100), nullable=True)               # denormalized for display
    name       = Column(String(120), nullable=False)
    page       = Column(String(40),  default="open-items")        # which screen
    query      = Column(Text,        nullable=True)               # JSON filter dict
    is_shared  = Column(Boolean,     default=False)               # visible to other users
    created_at = Column(DateTime,    default=datetime.datetime.utcnow)

    __table_args__ = (
        Index("ix_savedview_user_page", "user_id", "page"),
    )


class WatchFolderConfig(Base):
    """
    One row per upload type (fino/bank, fino/internal, airtel/bank, airtel/internal).
    Stores the folder path + filename pattern the auto-upload service uses to
    locate today's file.
    """
    __tablename__ = "watch_folder_configs"

    id               = Column(String(36),   primary_key=True, default=generate_id)
    label            = Column(String(100),  nullable=False)    # e.g. "Fino Bank Statement"
    partner          = Column(String(50),   nullable=False)    # fino / airtel
    side             = Column(String(20),   nullable=False)    # bank / internal
    folder_path      = Column(String(1000), nullable=True)     # absolute path on server machine
    file_prefix      = Column(String(200),  default="")        # e.g. "FINO_BANK_"
    file_suffix      = Column(String(50),   default=".xlsx")   # e.g. ".xlsx"
    date_format      = Column(String(20),   default="YYYYMMDD") # YYYYMMDD | YYYY-MM-DD | DD-MM-YYYY | DDMMYYYY
    recon_date_mode  = Column(String(20),   default="auto")    # auto | fixed
    auto_recon       = Column(Boolean,      default=True)
    is_enabled       = Column(Boolean,      default=True)
    created_by       = Column(String(36),   ForeignKey("users.id"), nullable=True)
    created_at       = Column(DateTime,     default=datetime.datetime.utcnow)
    updated_at       = Column(DateTime,     default=datetime.datetime.utcnow)
    # Last trigger result (manual or scheduled)
    last_triggered_at     = Column(DateTime,  nullable=True)
    last_trigger_status   = Column(String(20), nullable=True)  # success | error | not_found
    last_trigger_message  = Column(Text,       nullable=True)
    last_trigger_filename = Column(String(500), nullable=True)


# ─── Partner & Format Config (Admin-managed) ──────────────────────────────────

class PartnerConfig(Base):
    """
    One row per banking partner / product.
    Drives Upload dropdowns, RunRecon selectors, and auto-upload cards.
    Admins can add/edit/disable partners without touching code.
    """
    __tablename__ = "partner_configs"

    id                 = Column(String(36),  primary_key=True, default=generate_id)
    slug               = Column(String(50),  unique=True, nullable=False)   # fino, airtel, axis
    display_name       = Column(String(100), nullable=False)                # Fino Bank
    match_prefix       = Column(String(5),   nullable=False)                # FNO (for match IDs)
    product            = Column(String(20),  nullable=False, default="dmt") # dmt | aeps | pg
    source_value       = Column(String(50),  nullable=True)                 # value in Simplibank Source col
    has_bank_statement = Column(Boolean,     default=True)                  # shows bank upload card
    has_internal_dump  = Column(Boolean,     default=True)
    is_active          = Column(Boolean,     default=True)
    sort_order         = Column(Integer,     default=0)
    notes              = Column(Text,        nullable=True)
    # Cross-date settlement carry-forward window (days). 1 = NEFT D+1 (default),
    # 2 = T+2 settlement partners, 0 = same-day only. Used by the carry-forward matcher.
    settlement_carry_days = Column(Integer,  default=1)
    created_at         = Column(DateTime,    default=datetime.datetime.utcnow)
    updated_at         = Column(DateTime,    default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class BankAccount(Base):
    """
    A physical bank account belonging to a DMT partner (e.g. Axis has several
    current accounts). Bank statements are tagged with their account number so a
    single partner can ingest multiple statements and still be filtered/reported
    per account. Recon itself stays partner-level (matching is account-agnostic).
    Admins can register new accounts from Configuration → Bank Accounts.
    """
    __tablename__ = "bank_accounts"

    id             = Column(String(36),  primary_key=True, default=generate_id)
    partner        = Column(String(50),  nullable=False, index=True)   # axis, fino, airtel…
    account_number = Column(String(40),  nullable=False, unique=True)  # full account number
    label          = Column(String(120), nullable=True)                # e.g. "Axis DMT — A/c 4138"
    ifsc           = Column(String(20),  nullable=True)
    is_primary     = Column(Boolean,     default=False)                # the originally integrated account
    is_active      = Column(Boolean,     default=True)
    auto_added     = Column(Boolean,     default=False)                # discovered from an upload vs added by admin
    created_at     = Column(DateTime,    default=datetime.datetime.utcnow)
    updated_at     = Column(DateTime,    default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class BankFormatPreset(Base):
    """
    Column-mapping preset for a partner + side combination.
    Replaces the hardcoded BANK_FORMAT_PRESETS dict in upload.py over time.
    Existing hardcoded presets remain as fallback; DB rows take priority when present.
    """
    __tablename__ = "bank_format_presets_db"

    id             = Column(String(36),  primary_key=True, default=generate_id)
    partner_slug   = Column(String(50),  nullable=False)
    side           = Column(String(20),  nullable=False)    # bank | internal
    label          = Column(String(200), nullable=False)
    # JSON list of lowercased column names that uniquely identify this file format
    signature_cols = Column(Text,        nullable=False)
    # JSON dict: {std_field → file_column}  e.g. {"eko_tid": "Partner_txn_id"}
    column_mapping = Column(Text,        nullable=False)
    # JSON dict for special handling: debit_col, credit_col, dr_cr_col, amount_col,
    # net_amount_col, typename_filter, source_filter, debit_credit_col, etc.
    special_config = Column(Text,        nullable=True)
    is_active      = Column(Boolean,     default=True)
    created_at     = Column(DateTime,    default=datetime.datetime.utcnow)
    updated_at     = Column(DateTime,    default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    __table_args__ = (
        Index("ix_bfp_partner_side", "partner_slug", "side"),
    )


class UploadSchedule(Base):
    """
    One row per WatchFolderConfig that has a cron schedule enabled.
    Fires daily at the configured hour:minute.
    """
    __tablename__ = "upload_schedules"

    id               = Column(String(36),  primary_key=True, default=generate_id)
    watch_folder_id  = Column(String(36),  ForeignKey("watch_folder_configs.id"), unique=True)
    hour             = Column(Integer,     default=8)      # 0–23
    minute           = Column(Integer,     default=0)      # 0–59
    is_enabled       = Column(Boolean,     default=False)
    created_at       = Column(DateTime,    default=datetime.datetime.utcnow)
    last_run_at      = Column(DateTime,    nullable=True)
    last_run_status  = Column(String(20),  nullable=True)  # success | error | not_found
    last_run_message = Column(Text,        nullable=True)


class ReportSubscription(Base):
    """
    Personal scheduled report — one row per report a user wants delivered
    automatically. Each user manages their own; admins can see all.

    Fires daily / weekly / monthly at hour:minute (Asia/Kolkata) and emails
    an Excel attachment. The cron job is registered in APScheduler via
    core.report_scheduler.register_report_job() on create / update / toggle,
    and re-seeded from these rows on startup.
    """
    __tablename__ = "report_subscriptions"

    id           = Column(String(36),  primary_key=True, default=generate_id)
    user_id      = Column(String(36),  ForeignKey("users.id"), index=True)
    username     = Column(String(100), nullable=True)        # snapshot for display
    name         = Column(String(200), nullable=False)       # user-facing label

    report_type  = Column(String(30),  nullable=False)       # open_items | summary | eod | matched_pairs | ageing | src
    filters      = Column(Text,        default="{}")         # JSON: partner / side / src_code / recon_status
    date_range   = Column(String(20),  default="yesterday")  # today | yesterday | last_7_days | this_week | this_month | last_month

    frequency    = Column(String(10),  default="daily")      # daily | weekly | monthly
    hour         = Column(Integer,     default=8)            # 0–23 (IST)
    minute       = Column(Integer,     default=0)            # 0–59
    day_of_week  = Column(Integer,     nullable=True)         # 0=Mon … 6=Sun (weekly only)
    day_of_month = Column(Integer,     nullable=True)         # 1–31 (monthly only)

    email_to     = Column(String(500), nullable=True)        # comma-separated recipients

    # Opt-in: also embed the executive analytics dashboard (KPIs + per-product match
    # rates + link to the live dashboard) into the email body. Works with ANY
    # report_type — the report's own date window is used for the dashboard too.
    include_dashboard = Column(Boolean, default=False)

    is_active    = Column(Boolean,     default=True)
    last_run_at      = Column(DateTime, nullable=True)
    last_run_status  = Column(String(20), nullable=True)     # success | error | skipped_no_smtp
    last_run_message = Column(Text,       nullable=True)
    created_at   = Column(DateTime,    default=datetime.datetime.utcnow)


class DashboardOtp(Base):
    """
    One-time email codes for passwordless @eko.co.in access to the executive
    dashboard (/exec). A visitor enters their @eko.co.in email, receives a 6-digit
    code, and exchanges it for a short-lived VIEWER session (dashboard-only). The
    code is stored only as a SHA-256 hash; single-use; time-boxed. New table —
    created by create_all(), no migration needed.
    """
    __tablename__ = "dashboard_otps"

    id         = Column(String(36),  primary_key=True, default=generate_id)
    email      = Column(String(200), index=True, nullable=False)
    code_hash  = Column(String(64),  nullable=False)        # sha256 of the 6-digit code
    expires_at = Column(DateTime,    nullable=False)
    used       = Column(Boolean,     default=False)
    attempts   = Column(Integer,     default=0)
    created_at = Column(DateTime,    default=datetime.datetime.utcnow)


class LoginOtp(Base):
    """
    Second-factor email codes for the app LOGIN (username + password + emailed code).
    A code is created only AFTER the password is verified, sent to the user's
    @eko.co.in email, single-use, time-boxed, attempt-capped. New table —
    created by create_all(), no migration needed.
    """
    __tablename__ = "login_otps"

    id         = Column(String(36),  primary_key=True, default=generate_id)
    username   = Column(String(100), index=True, nullable=False)
    email      = Column(String(200), nullable=False)
    code_hash  = Column(String(64),  nullable=False)
    expires_at = Column(DateTime,    nullable=False)
    used       = Column(Boolean,     default=False)
    attempts   = Column(Integer,     default=0)
    created_at = Column(DateTime,    default=datetime.datetime.utcnow)


# ─── AePS Settlement Recon Models ─────────────────────────────────────────────

class AepsSettlement(Base):
    """
    One row per T Plus settlement batch from Fingpay.
    Captures the complete settlement breakdown for formula verification.
    """
    __tablename__ = "aeps_settlements"

    id                 = Column(String(36), primary_key=True, default=generate_id)
    upload_date        = Column(String(10))            # YYYY-MM-DD when we uploaded
    created_date       = Column(String(10), index=True) # T Plus "Created Date"
    settle_timestamp   = Column(String(30))            # "Settle Timestamp"
    settlement_amount  = Column(MONEY, default=0)      # what Fingpay credits us
    transaction_amount = Column(MONEY, default=0)      # gross txn volume
    anomaly_amount     = Column(MONEY, default=0)      # 3Way Anomaly deduction
    hold_recovery      = Column(MONEY, default=0)      # Hold Amount/Recovery
    twafa_per_txn      = Column(MONEY, default=0)      # per-txn 2FA charge (₹)
    twafa_count        = Column(Integer, default=0)    # number of 2FA txns
    twafa_total        = Column(MONEY, default=0)      # total 2FA deduction (incl. GST)
    cd_amount          = Column(MONEY, default=0)      # chargeback deductions
    reference_number   = Column(String(100))           # CWP... payment reference
    cms_number         = Column(String(100))
    status_message     = Column(String(500))
    service_type       = Column(String(50))
    status             = Column(String(50))            # Paid / Pending
    uploaded_by        = Column(String(50))
    created_at         = Column(DateTime, default=datetime.datetime.utcnow)


class AepsAnomaly(Base):
    """
    One row per unsettled/anomaly transaction from Fingpay Anomaly RRN report.
    RRN links back to Transaction.tracking_number on the bank side.
    """
    __tablename__ = "aeps_anomalies"

    id                = Column(String(36), primary_key=True, default=generate_id)
    upload_date       = Column(String(10))
    rrn               = Column(String(50), index=True)  # = tracking number
    amount            = Column(MONEY, default=0)
    original_status   = Column(String(20))
    threeway_status   = Column(String(20))
    txn_timestamp     = Column(String(30))              # Requested Timestamp
    created_timestamp = Column(String(30))              # Created Timestamp (settled)
    service_type      = Column(String(50))
    active_flag       = Column(String(10))
    uploaded_by       = Column(String(50))
    created_at        = Column(DateTime, default=datetime.datetime.utcnow)


class AepsCIB(Base):
    """
    One row per chargeback or penalty entry from Fingpay CIB report.
    RRN links back to Transaction.tracking_number on the bank side.
    CD Amount in T Plus = sum of these entries for the settlement period.
    """
    __tablename__ = "aeps_cib"

    id               = Column(String(36), primary_key=True, default=generate_id)
    upload_date      = Column(String(10))
    recovery_date    = Column(String(10), index=True)   # date deducted from settlement
    rrn              = Column(String(50), index=True)   # = tracking number
    amount           = Column(MONEY, default=0)         # deducted amount
    anomaly_recovery = Column(MONEY, default=0)         # anomaly Recovery Amount
    product          = Column(String(50))               # CW / etc.
    reason           = Column(String(200))              # Chargeback Raise / PENALTY
    dispute_type     = Column(String(50))               # CHARGEBACK / FRAUD
    txn_date         = Column(String(30))               # original txn date
    uploaded_by      = Column(String(50))
    created_at       = Column(DateTime, default=datetime.datetime.utcnow)


# ─── PI Integrity Check & Settlement Bank Config ─────────────────────────────

class PIIntegrityCheck(Base):
    """
    AS=SD cross-file integrity check state for AePS (Fingpay cw file vs T Plus).
    One row per (partner, recon_date). Updated each time either file is uploaded.
    PayU AS=SD is per-upload (in-memory only, not persisted).
    """
    __tablename__ = "pi_integrity_checks"

    id                = Column(String(36), primary_key=True, default=generate_id)
    partner           = Column(String(20), index=True)   # 'aeps'
    recon_date        = Column(String(10), index=True)   # YYYY-MM-DD (from Fingpay session.recon_date)
    cw_amount         = Column(MONEY, nullable=True)     # sum(Transaction Amount, Status=Success) from Fingpay cw file
    cw_row_count      = Column(Integer, nullable=True)   # number of Success rows in cw file
    cw_uploaded_at    = Column(DateTime, nullable=True)
    tplus_amount      = Column(MONEY, nullable=True)     # Transaction Amount from T Plus settlement batch
    tplus_batch_date  = Column(String(10), nullable=True)  # T Plus Created Date
    tplus_uploaded_at = Column(DateTime, nullable=True)
    # pending_cw | pending_tplus | passed | failed
    status            = Column(String(20), default="pending_cw")
    difference        = Column(MONEY, nullable=True)
    tolerance         = Column(MONEY, default=1.0)       # rupee tolerance for rounding
    updated_at        = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("partner", "recon_date", name="uq_pi_integrity_partner_date"),
    )


class SettlementBankConfig(Base):
    """
    Settlement bank account configuration for two-stage SEV (Settlement Verification).
    Maps each PI partner to the bank account where their settlements land,
    and the narration keyword used to identify their credits in that bank statement.
    Editable at any time since settlement accounts can change.
    """
    __tablename__ = "settlement_bank_configs"

    id                 = Column(String(36), primary_key=True, default=generate_id)
    partner            = Column(String(50), unique=True, index=True)  # 'pg', 'aeps', 'qr'
    partner_label      = Column(String(100))            # e.g. "PayU (Accept Payment)"
    bank_name          = Column(String(100))            # e.g. "Axis Bank"
    account_number     = Column(String(50))             # e.g. "000011112222333"
    narration_keyword  = Column(String(200))            # e.g. "GATEWAY SETTLEMENT" — identifies PI credit in bank statement
    settlement_period  = Column(String(20))             # e.g. "T+2", "T+1"
    is_active          = Column(Boolean, default=True)
    notes              = Column(Text, nullable=True)
    created_at         = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at         = Column(DateTime, default=datetime.datetime.utcnow)


# ─── SBI Kiosk Banking Reconciliation Models ─────────────────────────────────

class SBIBankTransaction(Base):
    """
    SBI Bank Statement row (kiosk settlement account).
    Parsed from tab-separated .xls file.
    Settlement rows identified by EKOSETTLEMENT keyword in description.
    """
    __tablename__ = "sbi_bank_transactions"

    id            = Column(String(36), primary_key=True, default=generate_id)
    upload_date   = Column(String(10), index=True)
    txn_date      = Column(String(10), index=True)
    value_date    = Column(String(10))
    description   = Column(Text)
    ref_number    = Column(String(100), index=True)   # 20-digit ref extracted from description
    branch_code   = Column(String(20))
    debit         = Column(MONEY, default=0)
    credit        = Column(MONEY, default=0)
    balance       = Column(MONEY)
    # Derived fields (extracted from description)
    ko_id         = Column(String(20), index=True)    # KO/CSP ID from description
    deduct_date   = Column(String(10))                # Wallet deduction date (from EKOSETTLEMENT desc)
    is_settlement = Column(Boolean, default=False)    # True if EKOSETTLEMENT keyword present
    txn_type      = Column(String(30))                # MoneyTRF, AEPSWDL, Settlement, etc.
    created_at    = Column(DateTime, default=datetime.datetime.utcnow)


class SBITxnReport(Base):
    """
    SBI BC Transaction Report row (from any of the 7 transaction type files).
    All 7 files share the same column structure.
    """
    __tablename__ = "sbi_txn_reports"

    id              = Column(String(36), primary_key=True, default=generate_id)
    upload_date     = Column(String(10), index=True)
    source_file     = Column(String(100))             # which of the 7 files this came from
    ko_id           = Column(String(20), index=True)
    txn_datetime    = Column(String(30))
    txn_date        = Column(String(10), index=True)
    reference_number= Column(String(100), index=True)
    txn_type        = Column(String(80))              # AEPS OFFUS Withdrawal, Money Transfer, etc.
    from_account    = Column(String(30))
    to_account      = Column(String(30))
    amount          = Column(MONEY, default=0)
    customer_charge = Column(MONEY, default=0)
    journal_number  = Column(String(30))
    status          = Column(String(20))              # Success / Failure
    reversal_status = Column(String(20))
    settlement_acct = Column(String(20))              # BC / etc. from SETTELMENT_ACCOUNT_
    ko_holding      = Column(MONEY)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)


class SBIKOLimits(Base):
    """
    KO Limits Configuration Report row.
    Records every KO Deposit and KO Withdrawal transaction (wallet in/out).
    Used in P01 (settlement recon) and P04 (balance recon).
    """
    __tablename__ = "sbi_ko_limits"

    id              = Column(String(36), primary_key=True, default=generate_id)
    upload_date     = Column(String(10), index=True)
    txn_datetime    = Column(String(30))
    txn_date        = Column(String(10), index=True)
    limit_configured_by = Column(String(20))
    ko_id           = Column(String(20), index=True)
    opening_limit   = Column(MONEY)
    txn_type        = Column(String(30))              # KO Deposit / KO Withdrawal
    amount          = Column(MONEY, default=0)
    closing_limit   = Column(MONEY)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)


class SBIKOCashHolding(Base):
    """
    KO Cash Holding Report row.
    Daily snapshot of each KO's wallet balance.
    Used in P04 (balance recon).
    """
    __tablename__ = "sbi_ko_cash_holding"

    id              = Column(String(36), primary_key=True, default=generate_id)
    upload_date     = Column(String(10), index=True)
    report_date     = Column(String(10), index=True)
    ko_id           = Column(String(20), index=True, unique=False)
    limit           = Column(MONEY)
    opening_balance = Column(MONEY)
    cash_receipts   = Column(MONEY, default=0)
    cash_payments   = Column(MONEY, default=0)
    ko_deposit      = Column(MONEY, default=0)
    ko_withdrawal   = Column(MONEY, default=0)
    closing_balance = Column(MONEY)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)


class SBILimitFailure(Base):
    """
    Limit Update Failure Report row.
    Records KO wallets where a deposit/withdrawal limit update FAILED.
    Compared against KO Cash Holding in P04 to identify adjustment needs.
    """
    __tablename__ = "sbi_limit_failures"

    id              = Column(String(36), primary_key=True, default=generate_id)
    upload_date     = Column(String(10), index=True)
    txn_date        = Column(String(10), index=True)
    csp_code        = Column(String(20), index=True)
    bc_id           = Column(String(20))
    amount          = Column(MONEY)               # negative = withdrawal, positive = deposit
    user            = Column(String(50))
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)


class SBICSPMaster(Base):
    """
    CSP Master Sheet row — maps CSP Code to Reference Number and Mode.
    Cash: fixed ref per CSP (same ref across multiple deposits).
    Electronic/CDM: each row is a unique transaction reference.
    Used in P03 (CSP-Transaction-Bank recon).
    """
    __tablename__ = "sbi_csp_master"

    id          = Column(String(36), primary_key=True, default=generate_id)
    upload_date = Column(String(10), index=True)
    csp_code    = Column(String(20), index=True)
    ref_number  = Column(String(100), index=True)
    mode        = Column(String(30))              # Cash / CDM / Electronic / Check Deposit
    created_at  = Column(DateTime, default=datetime.datetime.utcnow)


class SBIP01Result(Base):
    """
    P01 SBI Settlement Reconciliation result — one row per KO per reconciliation run.
    Compares KO wallet withdrawal (Limits Config) vs bank settlement credit (bank statement).
    """
    __tablename__ = "sbi_p01_results"

    id              = Column(String(36), primary_key=True, default=generate_id)
    recon_date      = Column(String(10), index=True)
    ko_id           = Column(String(20), index=True)
    wallet_withdrawn= Column(MONEY, default=0)    # from KO Limits Config KO Withdrawal
    bank_settled    = Column(MONEY, default=0)    # from Bank Statement EKOSETTLEMENT
    difference      = Column(MONEY, default=0)
    status          = Column(String(20))          # matched / unmatched (since 2026-07-27)
    deduct_date     = Column(String(10))          # business date (from bank description); P01 matches on this
    bank_txn_date   = Column(String(10))          # actual bank statement date
    notes           = Column(Text)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)


class SBIP02Result(Base):
    """
    P02 Bank Statement & Transaction Report Reconciliation result.
    One row per bank transaction, showing match status against transaction reports.
    """
    __tablename__ = "sbi_p02_results"

    id              = Column(String(36), primary_key=True, default=generate_id)
    recon_date      = Column(String(10), index=True)
    bank_txn_id     = Column(String(36))          # FK to sbi_bank_transactions.id
    txn_report_id   = Column(String(36))          # FK to sbi_txn_reports.id (if matched)
    reference_number= Column(String(100), index=True)
    ko_id           = Column(String(20))
    bank_amount     = Column(MONEY)
    bank_type       = Column(String(5))           # DR / CR
    report_amount   = Column(MONEY)
    report_txn_type = Column(String(80))
    match_status    = Column(String(20))          # Matched / Unmatched / Partial / Reversal
    reversal_type   = Column(String(20))          # Reversal Debit / Reversal Credit / No
    success_status  = Column(String(10))          # Success / Fail
    notes           = Column(Text)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)


class SBIP03Result(Base):
    """
    P03 CSP-Transaction-Bank Reconciliation result.
    Matches money-out (Transaction Report debits) vs money-in (Bank Statement credits).
    """
    __tablename__ = "sbi_p03_results"

    id              = Column(String(36), primary_key=True, default=generate_id)
    recon_date      = Column(String(10), index=True)
    csp_code        = Column(String(20), index=True)
    mode            = Column(String(30))          # Cash / CDM / Electronic
    ref_number      = Column(String(100))
    txn_amount      = Column(MONEY)               # from Transaction Report
    txn_date        = Column(String(10))
    bank_credit_date= Column(String(10))          # date money received in bank
    bank_amount     = Column(MONEY)
    match_status    = Column(String(20))          # Matched / Unmatched_TxnReport / Unmatched_Bank
    date_shift      = Column(Integer, nullable=True)   # 0=same day, 1=D+1, -1=D-1, None=unmatched
    match_priority  = Column(Integer)             # 1-4 per SOP priority order
    notes           = Column(Text)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)


class SBIP04Result(Base):
    """
    P04 CSP Wallet Balance Reconciliation result.
    Compares Limit Update Failures against KO Cash Holding closing balance.
    """
    __tablename__ = "sbi_p04_results"

    id              = Column(String(36), primary_key=True, default=generate_id)
    recon_date      = Column(String(10), index=True)
    csp_code        = Column(String(20), index=True)
    failed_amount   = Column(MONEY)               # from Limit Fail Report
    closing_balance = Column(MONEY)               # from KO Cash Holding
    expected_balance= Column(MONEY)               # closing_balance + failed_amount (if withdrawal) or - (if deposit)
    difference      = Column(MONEY)
    action_required = Column(String(20))          # DEPOSIT / WITHDRAWAL / NONE
    action_amount   = Column(MONEY, default=0)
    action_done     = Column(Boolean, default=False)  # team marks done after SBI portal action
    notes           = Column(Text)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)


class SBIManualMatch(Base):
    """
    Operator manual match for SBI Kiosk recon (persistent overlay, additive).

    P01–P04 results are delete-and-recreated on every run (behavior-contract #17),
    so a manual match can't live in the result row — it would be wiped. It is stored
    here keyed by the row's stable BUSINESS key (not its regenerated id) and overlaid
    onto the results + exports at READ time, so it survives re-runs without touching
    the run logic. Deletable by an operator to undo.
    """
    __tablename__ = "sbi_manual_matches"

    id              = Column(String(36),  primary_key=True, default=generate_id)
    recon_date      = Column(String(10),  index=True)
    process         = Column(String(5),   index=True)      # p02 | p03
    match_key       = Column(String(200), index=True)      # stable business key of the row
    counterpart_ref = Column(String(120), nullable=True)   # what it was matched against
    remark          = Column(String(500))
    created_by      = Column(String(100))
    created_at      = Column(DateTime,    default=datetime.datetime.utcnow)

    __table_args__ = (
        Index("ix_sbimm_lookup", "recon_date", "process", "match_key"),
    )


class SBISrcAssignment(Base):
    """
    Operator SRC disposition for SBI Kiosk recon (persistent overlay, additive).

    Like SBIManualMatch, an SRC tag can't live on the P01–P04 result rows because
    those are delete-and-recreated on every run (behavior-contract #17). It is stored
    here keyed by the row's stable BUSINESS key and overlaid onto results + exports at
    READ time, so a tagged SRC survives re-runs without touching the run logic.
    Deletable by an operator to undo. Mirrors core-ledger assign-src (src_code+src_note).
    """
    __tablename__ = "sbi_src_assignments"

    id           = Column(String(36),  primary_key=True, default=generate_id)
    recon_date   = Column(String(10),  index=True)
    process      = Column(String(5),   index=True)      # p01 | p02 | p03 | p04
    match_key    = Column(String(200), index=True)      # stable business key of the row
    src_code     = Column(String(50))
    src_note     = Column(String(500), nullable=True)
    created_by   = Column(String(100))
    created_at   = Column(DateTime,    default=datetime.datetime.utcnow)

    __table_args__ = (
        Index("ix_sbisrc_lookup", "recon_date", "process", "match_key"),
    )


class SBIManualPair(Base):
    """
    Operator two-sided manual PAIR for SBI Kiosk recon (persistent overlay, additive).

    Unlike SBIManualMatch (a one-sided status flip on a single P02/P03 result row),
    this links ONE bank-side row to ONE internal/data-side row — the pair-picker window
    ("SBI Kiosk → Manual Match"). Both source rows (bank statement, txn report, KO limits)
    survive recon re-runs, but their ids are regenerated on file RE-UPLOAD, so — like
    SBIManualMatch/SBISrcAssignment (behavior-contract #17) — the link is keyed by each
    side's STABLE BUSINESS key, computed identically at write time (from the source row)
    and read time (from the unified entry). Overlaid onto the unified read model at READ
    time so both rows flip to "Manual_Matched" and show each other as counterpart; it never
    touches the P01–P04 run logic. Deletable by an operator to undo.

    Free-form: any bank row may be paired with any internal row (settlement↔KO withdrawal,
    bank↔txn report, bank credit↔txn report, etc.); an amount/bucket mismatch is surfaced
    as a warning, not blocked. Bank and data business dates may differ (D+1 settlements).
    """
    __tablename__ = "sbi_manual_pairs"

    id           = Column(String(36),  primary_key=True, default=generate_id)
    bank_date    = Column(String(10),  index=True)       # business date of the bank row
    data_date    = Column(String(10),  index=True)       # business date of the data row
    bank_key     = Column(String(200), index=True)       # stable business key, bank side
    data_key     = Column(String(200), index=True)       # stable business key, data side
    bank_source  = Column(String(30))                    # Bank Settlement | Bank Statement
    data_source  = Column(String(30))                    # Txn Report | KO Withdrawal | KO Deposit
    bank_amount  = Column(MONEY)
    data_amount  = Column(MONEY)
    remark       = Column(String(500))
    created_by   = Column(String(100))
    created_at   = Column(DateTime,    default=datetime.datetime.utcnow)

    __table_args__ = (
        Index("ix_sbipair_bank", "bank_key"),
        Index("ix_sbipair_data", "data_key"),
    )


# ─── QR Collection Settlement & Chargeback Models ─────────────────────────────

class QRSettlement(Base):
    """
    One row per QR settlement batch from the partner (ExportSettlementsList).
    Formula: Net = Amount − Fees − Fees Tax − Early Settlement − Early Settlement Tax
    """
    __tablename__ = "qr_settlements"

    id                    = Column(String(36), primary_key=True, default=generate_id)
    upload_date           = Column(String(10))
    settlement_id         = Column(String(50), index=True)
    payment_date          = Column(String(30))
    gross_amount          = Column(MONEY, default=0)   # "Amount"
    fees                  = Column(MONEY, default=0)
    fees_tax              = Column(MONEY, default=0)   # "Fees Tax Amount"
    early_settlement_fees = Column(MONEY, default=0)
    early_settlement_tax  = Column(MONEY, default=0)
    net_settlement        = Column(MONEY, default=0)   # "Net Settlement Amount"
    mode                  = Column(String(20))         # IMPS / RTGS
    payment_status        = Column(String(30))         # Approved
    payout_status         = Column(String(30))         # processed / reversed
    payout_remarks        = Column(String(200))
    bank_ref_number       = Column(String(100))
    rrn                   = Column(String(50), index=True)  # settlement UTR
    uploaded_by           = Column(String(50))
    created_at            = Column(DateTime, default=datetime.datetime.utcnow)


class QRChargeback(Base):
    """
    Manual chargeback entries entered from Paypoint chargeback emails.
    RRN cross-references bank transaction table to identify original transaction.
    """
    __tablename__ = "qr_chargebacks"

    id              = Column(String(36), primary_key=True, default=generate_id)
    case_date       = Column(String(10), index=True)   # Case Received date
    adj_type        = Column(String(100))              # Complaint Raise / etc.
    txn_date        = Column(String(10))               # Txndate
    upi_txn_id      = Column(String(200))              # UPI Txn ID (alphanumeric)
    rrn             = Column(String(50), index=True)   # RRN = bank tracking_number
    adj_amount      = Column(MONEY, default=0)         # Adjamount
    tat_date        = Column(String(10))               # TAT deadline
    status          = Column(String(30), default="pending")  # pending / settled / defended
    notes           = Column(Text, nullable=True)
    created_by      = Column(String(50))
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


# ─── E-Value (Wallet Load) Reconciliation Models ──────────────────────────────

class EvalueAccount(Base):
    """
    Master list of our bank accounts used for E-Value (wallet load) collection.
    Seeded from BANK DETAILS. reco_acc_no (e.g. 'SBI-4393') is the canonical key
    that appears as `source` in the internal SMB_BANK_LOADIN dump.
    """
    __tablename__ = "evalue_accounts"

    id             = Column(String(36), primary_key=True, default=generate_id)
    bank_name      = Column(String(100), nullable=False)       # e.g. "SBI"
    account_number = Column(String(40),  nullable=False)
    reco_acc_no    = Column(String(40),  unique=True, nullable=False)  # e.g. "SBI-4393"
    is_active      = Column(Boolean,     default=True)
    created_at     = Column(DateTime,    default=datetime.datetime.utcnow)

    __table_args__ = (Index("ix_evacct_bank", "bank_name"),)


class EvalueWalletLoad(Base):
    """
    One row per internal wallet-load request (SMB_BANK_LOADIN dump).
    These are the CSP load requests we reconcile against bank credits.
    """
    __tablename__ = "evalue_wallet_loads"

    id              = Column(String(36), primary_key=True, default=generate_id)
    upload_date     = Column(String(10), index=True)          # YYYY-MM-DD ingested
    reco_acc_no     = Column(String(40), index=True)          # = source
    eko_trxn_id     = Column(String(40), index=True)
    account_trxn_id = Column(String(40))
    csp_code        = Column(String(40), index=True)
    merchant_name   = Column(String(200))
    cell_number     = Column(String(20), index=True)
    amount          = Column(MONEY, default=0)
    transaction_date= Column(String(10), index=True)
    value_date      = Column(String(10))
    status          = Column(String(30))
    typename        = Column(String(50))
    dr_cr           = Column(String(5))
    mode            = Column(String(30))
    load_mode       = Column(String(10))                      # ONLINE | CASH (derived)
    utr_number      = Column(String(60), index=True)
    tid_chequeno    = Column(String(60))
    bank_ref        = Column(String(120), index=True)         # Is_Weekend_Loan col — full bank reference (e.g. HDFCR…796815)
    cdm_txn_number  = Column(String(60))
    cdm_branch      = Column(String(120))
    branch_name     = Column(String(120))
    branch_code     = Column(String(40))
    remarks         = Column(Text)
    comments        = Column(Text)
    provider_type   = Column(String(20))
    # recon state
    recon_status    = Column(String(20), default="unmatched_load")
    match_id        = Column(String(40), index=True)
    match_note      = Column(String(300))
    # human-override trail
    override_by       = Column(String(100), nullable=True)
    override_at       = Column(DateTime,    nullable=True)
    prev_recon_status = Column(String(30),  nullable=True)
    # SRC disposition (parity with core-ledger assign-src)
    src_code          = Column(String(50),  nullable=True)
    src_note          = Column(String(500), nullable=True)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        Index("ix_evload_acct_date", "reco_acc_no", "transaction_date"),
        Index("ix_evload_status", "recon_status"),
    )

    @property
    def recon_date_effective(self):
        """The date this load RECONCILES on = its VALUE date, falling back to the
        transaction date only when value date is blank. The bank credits an E-Value
        load on its value date, so that is the date every data view / filter / ageing
        keys off; transaction_date stays stored as the initiation date. Finance ops /
        Rajendra — see behavior-contract item 13."""
        return self.value_date or self.transaction_date or ""


class EvalueBankTxn(Base):
    """
    One row per parsed bank-statement transaction for a selected bank account.
    Normalised from 8 heterogeneous bank formats by core.evalue_engine.
    """
    __tablename__ = "evalue_bank_txns"

    id           = Column(String(36), primary_key=True, default=generate_id)
    upload_date  = Column(String(10), index=True)
    bank_name    = Column(String(100), index=True)
    reco_acc_no  = Column(String(40), index=True)
    account_number = Column(String(40))
    txn_date     = Column(String(10), index=True)
    value_date   = Column(String(10))
    description  = Column(Text)
    dr_cr        = Column(String(5))                          # CR | DR
    amount       = Column(MONEY, default=0)
    balance      = Column(MONEY, nullable=True)
    ref_no       = Column(String(60))
    utr          = Column(String(60), index=True)
    atm_ref      = Column(String(60))
    branch       = Column(String(120))
    mobile       = Column(String(20))
    channel      = Column(String(20))                         # IMPS|UPI|NEFT|RTGS|INTRA|CDM|FEE|DEBIT|OTHER
    # recon state
    recon_status = Column(String(20), default="")            # matched_online|matched_cash|twice_credit|wrong_amount|unmatched_bank|transaction_fee|bank_debit
    match_id     = Column(String(40), index=True)
    match_note   = Column(String(300))
    # twice-credit recovery workflow: ""(n/a) | recovery_pending | recovered | waived
    recovery_status = Column(String(20), nullable=True)
    recovery_note   = Column(String(300), nullable=True)
    # human-override trail
    override_by       = Column(String(100), nullable=True)
    override_at       = Column(DateTime,    nullable=True)
    prev_recon_status = Column(String(30),  nullable=True)
    # SRC disposition (parity with core-ledger assign-src)
    src_code          = Column(String(50),  nullable=True)
    src_note          = Column(String(500), nullable=True)
    created_at   = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        Index("ix_evbank_acct_date", "reco_acc_no", "txn_date"),
        Index("ix_evbank_status", "recon_status"),
    )


def _load_instance_seed_accounts():
    """Load bank-account seed data from instance/seed_accounts.json.

    Real account numbers are instance data, not source code: the file is
    gitignored so it can never be published. Returns {} when absent — a fresh
    install simply starts with an empty registry, and accounts can be added in
    Configuration → Bank Accounts / E-Value Accounts (or by copying
    instance/seed_accounts.example.json to seed_accounts.json).
    """
    import json as _json
    path = os.path.join(os.path.dirname(__file__), "..", "instance", "seed_accounts.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        if not isinstance(data, dict):
            print("[seed] instance/seed_accounts.json must be a JSON object — ignoring it")
            return {}
        return data
    except FileNotFoundError:
        print(
            "[seed] instance/seed_accounts.json not found — bank/E-Value account "
            "registries left as-is. Copy instance/seed_accounts.example.json to "
            "seed it, or add accounts in Configuration."
        )
        return {}
    except Exception as e:
        print(f"[seed] Could not read instance/seed_accounts.json: {e}")
        return {}


def seed_bank_accounts(db):
    """Seed the DMT bank-account registry. Idempotent (keyed on account number).

    Account tagging lets one partner ingest several statements and report per
    account. Data comes from instance/seed_accounts.json (see loader above).
    """
    accounts = _load_instance_seed_accounts().get("dmt_bank_accounts", [])
    for row in accounts:
        acct = row.get("account_number")
        if not acct:
            continue
        exists = db.query(BankAccount).filter(BankAccount.account_number == acct).first()
        if not exists:
            db.add(BankAccount(partner=row.get("partner"), account_number=acct,
                               label=row.get("label"),
                               is_primary=bool(row.get("is_primary", False)),
                               is_active=True, auto_added=False))
    db.commit()


# The original 9 hard-coded SRC codes, now the seed for the managed catalog. Editing
# these labels is safe (idempotent by code); removing one here does NOT delete a code
# already in the DB (seed only adds), so a deployed catalog is never truncated.
DEFAULT_SRC_CODES = [
    ("UNCLAIMED",      "Unclaimed / no counterpart yet"),
    ("ADVANCE_CREDIT", "Advance credit received"),
    ("BANK_CHARGES",   "Bank charges / fees / GST"),
    ("TWICE_CREDITED", "Credited twice"),
    ("INTERNAL_TXN",   "Internal / own transaction"),
    ("DELAYED_TXN",    "Delayed settlement (D+n)"),
    ("DUPLICATE",      "Duplicate entry"),
    ("MISSING_TID",    "Missing / mismatched TID"),
    ("OTHER",          "Other (see note)"),
]


def seed_src_codes(db):
    """Seed the SRC reason-code catalog with the original 9 codes. Idempotent
    (keyed on code): adds a code only if absent, never overwrites an operator's
    edits or deactivations, and never deletes."""
    for code, label in DEFAULT_SRC_CODES:
        if not db.query(SrcCode).filter(SrcCode.code == code).first():
            db.add(SrcCode(code=code, label=label, is_active=True, created_by="system"))
    db.commit()


def seed_evalue_accounts(db):
    """Seed the E-Value bank account master. Idempotent (keyed on reco_acc_no).

    reco_acc_no must EXACTLY equal the internal dump's `source` column —
    E-Value matching joins on it, including deliberate label quirks. Data comes
    from instance/seed_accounts.json (see loader above).
    """
    accounts = _load_instance_seed_accounts().get("evalue_accounts", [])
    for row in accounts:
        reco = row.get("reco_acc_no")
        if not reco:
            continue
        if not db.query(EvalueAccount).filter(EvalueAccount.reco_acc_no == reco).first():
            db.add(EvalueAccount(bank_name=row.get("bank_name"),
                                 account_number=row.get("account_number"),
                                 reco_acc_no=reco))
    db.commit()


class BbpsInternal(Base):
    """One normalised record per eko_trxn_id from the Simplibank BBPS dump."""
    __tablename__ = "bbps_internal"

    id              = Column(String(36), primary_key=True, default=generate_id)
    upload_date     = Column(String(10), index=True)
    eko_trxn_id     = Column(String(40), index=True)
    provider        = Column(String(20), index=True)   # moneyart | levin
    source          = Column(String(50))
    amount          = Column(MONEY, default=0)
    status          = Column(String(20))               # Success | Refunded
    is_refunded     = Column(Boolean, default=False)
    transaction_date= Column(String(10), index=True)
    csp_code        = Column(String(40))
    merchant_name   = Column(String(200))
    cell_number     = Column(String(20))
    tracking_number = Column(String(60))
    recon_status    = Column(String(25), default="unmatched_internal")
    match_id        = Column(String(40), index=True)
    match_note      = Column(String(300))
    override_by       = Column(String(100), nullable=True)
    override_at       = Column(DateTime, nullable=True)
    prev_recon_status = Column(String(30), nullable=True)
    # SRC disposition (parity with core-ledger assign-src)
    src_code          = Column(String(50),  nullable=True)
    src_note          = Column(String(500), nullable=True)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (Index("ix_bbps_int_prov_date", "provider", "transaction_date"),
                      Index("ix_bbps_int_status", "recon_status"))


class BbpsBankTxn(Base):
    """One row per operator/aggregator transaction (Moneyart or Levin report)."""
    __tablename__ = "bbps_bank_txns"

    id              = Column(String(36), primary_key=True, default=generate_id)
    upload_date     = Column(String(10), index=True)
    provider        = Column(String(20), index=True)   # moneyart | levin
    client_ref      = Column(String(40), index=True)   # == internal eko_trxn_id
    operator_ref    = Column(String(60))
    order_id        = Column(String(60))
    amount          = Column(MONEY, default=0)
    status          = Column(String(20))               # Success | Failed
    transaction_date= Column(String(10), index=True)
    service_name    = Column(String(120))
    operator_name   = Column(String(80))
    reason          = Column(String(200))
    recharge_product= Column(String(120))
    recon_status    = Column(String(25), default="unmatched_bank")
    match_id        = Column(String(40), index=True)
    match_note      = Column(String(300))
    override_by       = Column(String(100), nullable=True)
    override_at       = Column(DateTime, nullable=True)
    prev_recon_status = Column(String(30), nullable=True)
    # SRC disposition (parity with core-ledger assign-src)
    src_code          = Column(String(50),  nullable=True)
    src_note          = Column(String(500), nullable=True)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (Index("ix_bbps_bank_prov_date", "provider", "transaction_date"),
                      Index("ix_bbps_bank_status", "recon_status"))


class SessionLog(Base):
    """
    Session activity log — one row per login / logout / session-expiry event.
    Complements audit_logs (which records actions) by recording who was
    logged in, from where, and when their session started/ended.
    """
    __tablename__ = "session_logs"

    id         = Column(String(36), primary_key=True, default=generate_id)
    user_id    = Column(String(36), index=True, nullable=True)
    username   = Column(String(100), index=True)
    event      = Column(String(20))     # login | logout | login_failed | expired
    ip_address = Column(String(64), nullable=True)
    user_agent = Column(String(300), nullable=True)
    detail     = Column(String(300), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)


# ─── Developer Portal AI agent (Phase 2 — additive, read-only feature) ──────────
class AgentChatSession(Base):
    """
    One conversation thread with the read-only Developer Portal agent.
    Additive feature, gated by the 'portal_access' permission. The agent never
    writes to business data; these tables only store the chat transcript itself.
    """
    __tablename__ = "agent_chat_sessions"

    id         = Column(String(36),  primary_key=True, default=generate_id)
    username   = Column(String(100), index=True)            # who owns this thread
    title      = Column(String(300), nullable=True)         # first user message, truncated
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)


class AgentChatMessage(Base):
    """A single turn in an AgentChatSession (role = user | assistant)."""
    __tablename__ = "agent_chat_messages"

    id          = Column(String(36),  primary_key=True, default=generate_id)
    session_id  = Column(String(36),  ForeignKey("agent_chat_sessions.id"), index=True)
    role        = Column(String(20))                        # user | assistant
    content     = Column(Text)                              # rendered text of the turn
    # JSON: tool calls made this turn (name + args + brief result) for transparency
    tool_trace  = Column(Text, nullable=True)
    created_at  = Column(DateTime, default=datetime.datetime.utcnow, index=True)

    __table_args__ = (Index("ix_agent_msg_session_ts", "session_id", "created_at"),)


class PortalRequest(Base):
    """
    A change/error/feature request raised through the Developer Portal — by an
    engineer directly or drafted by the read-only agent. This is the human-approval
    queue: filing a request changes NOTHING in the live system; a request only
    becomes action after a human with approval rights moves it to 'approved'.
    Isolated governance data — never touched by the matching engines.
    """
    __tablename__ = "portal_requests"

    id            = Column(String(36),  primary_key=True, default=generate_id)
    req_type      = Column(String(20),  index=True)   # bug | faulty_data | feature | change | other
    title         = Column(String(300))
    description   = Column(Text)
    proposed_change = Column(Text, nullable=True)      # agent's drafted change / diff / plan
    priority      = Column(String(10),  default="medium")   # low | medium | high
    status        = Column(String(15),  default="open", index=True)  # open|triaged|approved|rejected|done
    source        = Column(String(15),  default="manual")  # manual | agent
    created_by    = Column(String(100), index=True)
    agent_session_id = Column(String(36), nullable=True)   # link back to the chat, if agent-filed
    created_at    = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    updated_at    = Column(DateTime, default=datetime.datetime.utcnow)
    reviewed_by   = Column(String(100), nullable=True)
    reviewed_at   = Column(DateTime, nullable=True)
    review_note   = Column(String(1000), nullable=True)
    # Lightweight workflow (additive): who's on it + a link out to the tracker.
    assignee         = Column(String(100), nullable=True, index=True)
    github_issue_url = Column(String(500), nullable=True)


class PortalRequestComment(Base):
    """A discussion comment on a PortalRequest. Pure governance/collaboration
    data — never touches business records. Any portal user may comment; the
    approval decision still lives on the request's status."""
    __tablename__ = "portal_request_comments"

    id         = Column(String(36),  primary_key=True, default=generate_id)
    request_id = Column(String(36),  ForeignKey("portal_requests.id"), index=True)
    author     = Column(String(100))
    body       = Column(Text)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)

    __table_args__ = (Index("ix_portal_comment_req_ts", "request_id", "created_at"),)


class PortalAgentJob(Base):
    """
    A scheduled, autonomous run of the read-only portal agent (Phase 4). On its
    cron cadence the agent runs `prompt` and may file requests into the approval
    queue — it still cannot change live data; humans approve any resulting request.
    Reuses the in-process APScheduler (Asia/Kolkata).
    """
    __tablename__ = "portal_agent_jobs"

    id          = Column(String(36),  primary_key=True, default=generate_id)
    name        = Column(String(200))
    prompt      = Column(Text)                              # what to ask the agent each run
    frequency   = Column(String(10),  default="daily")     # daily | weekly
    hour        = Column(Integer, default=8)               # IST
    minute      = Column(Integer, default=0)
    day_of_week = Column(Integer, nullable=True)           # 0=Mon .. 6=Sun (weekly only)
    is_enabled  = Column(Boolean, default=True)
    created_by  = Column(String(100))
    created_at  = Column(DateTime, default=datetime.datetime.utcnow)
    last_run_at     = Column(DateTime, nullable=True)
    last_status     = Column(String(15), nullable=True)    # ok | error
    last_summary    = Column(Text, nullable=True)


class PortalAgentRun(Base):
    """One execution record of a PortalAgentJob (or a manual run-now)."""
    __tablename__ = "portal_agent_runs"

    id            = Column(String(36),  primary_key=True, default=generate_id)
    job_id        = Column(String(36),  ForeignKey("portal_agent_jobs.id"), index=True)
    trigger       = Column(String(12),  default="schedule")  # schedule | manual
    started_at    = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    finished_at   = Column(DateTime, nullable=True)
    status        = Column(String(15), default="running")    # running | ok | error
    summary       = Column(Text, nullable=True)              # the agent's answer
    tools_used    = Column(Text, nullable=True)              # JSON list of tool summaries
    requests_filed = Column(Integer, default=0)
    error         = Column(Text, nullable=True)


class BuilderTask(Base):
    """
    Phase 5 — a WRITE-CAPABLE build task run by the autonomous Builder Agent.
    Unlike PortalRequest (a proposal that changes nothing), a BuilderTask actually
    edits code on an isolated git branch, runs the mandatory gates
    (pytest / compileall / npm build / ruff / behavior-contract), and — only if
    every gate passes and the master switch is on — applies the change as a git
    commit and restarts the app. Every commit is revertible (one-click rollback).

    HARD SAFETY (enforced in core/builder_agent.py, never relaxed here):
      • secret files (.env / keys / seed_accounts.json / *.db) are un-writable;
      • a change that fails ANY gate is never applied;
      • the whole capability is behind the `portal_build` permission AND a master
        kill-switch that ships OFF.
    """
    __tablename__ = "builder_tasks"

    id          = Column(String(36),  primary_key=True, default=generate_id)
    title       = Column(String(300))
    instruction = Column(Text)                              # what the user asked for
    # planning | awaiting_input | building | gating | ready | applied | failed | rejected | rolled_back
    status      = Column(String(20),  default="planning", index=True)
    branch      = Column(String(160), nullable=True)       # builder/<id> git branch
    base_sha    = Column(String(40),  nullable=True)       # HEAD the branch forked from
    commit_sha  = Column(String(40),  nullable=True)       # commit applied to live, if any
    summary     = Column(Text, nullable=True)              # agent's own summary of the change
    plan        = Column(Text, nullable=True)              # agent's plan before editing
    diff_stat   = Column(Text, nullable=True)              # `git diff --stat` text
    files_changed = Column(Text, nullable=True)            # JSON list of {path, action, +, -}
    gate_results  = Column(Text, nullable=True)            # JSON {gate: {ok, detail}}
    gates_ok    = Column(Boolean, default=False)
    created_by  = Column(String(100), index=True)
    created_at  = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    updated_at  = Column(DateTime, default=datetime.datetime.utcnow)
    applied_by  = Column(String(100), nullable=True)
    applied_at  = Column(DateTime, nullable=True)
    rolled_back_by = Column(String(100), nullable=True)
    rolled_back_at = Column(DateTime, nullable=True)
    error       = Column(Text, nullable=True)


class BuilderMessage(Base):
    """A turn in a BuilderTask's conversation (the agent is chat-driven and asks
    clarifying questions before it acts). role = user | assistant | event."""
    __tablename__ = "builder_messages"

    id         = Column(String(36),  primary_key=True, default=generate_id)
    task_id    = Column(String(36),  ForeignKey("builder_tasks.id"), index=True)
    role       = Column(String(20))                        # user | assistant | event
    content    = Column(Text)
    tool_trace = Column(Text, nullable=True)               # JSON list of tool summaries
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)

    __table_args__ = (Index("ix_builder_msg_task_ts", "task_id", "created_at"),)


class RecycleBin(Base):
    """Soft-delete store. Rows removed by a Clear action are serialised here BEFORE the
    delete, so an accidental clear is recoverable instead of terminal.

    One Clear action = one `batch_id`, split into one row per (table, chunk) so a huge
    delete never builds a single oversized blob (max_allowed_packet) or a giant in-memory
    list. `payload` is gzipped JSON: [{column: value, ...}, ...] with values stringified
    for dates/decimals — restore feeds them straight back through the model constructor.

    Retention is enforced by purge_expired() (see core/recycle_bin.py); rows are NOT
    deleted implicitly by anything else.
    """
    __tablename__ = "recycle_bin"

    id          = Column(String(36),  primary_key=True, default=generate_id)
    batch_id    = Column(String(36),  index=True)       # groups every table/chunk of one Clear
    module      = Column(String(20),  index=True)       # sbi | evalue | bbps | core
    table_name  = Column(String(64),  index=True)
    chunk_no    = Column(Integer,     default=0)
    row_count   = Column(Integer,     default=0)
    payload     = Column(BIGBLOB)                       # gzip(JSON rows)
    filters     = Column(Text,        nullable=True)    # JSON of what the user asked for
    reason      = Column(String(300), nullable=True)
    deleted_by  = Column(String(100), index=True)
    deleted_at  = Column(DateTime,    default=datetime.datetime.utcnow, index=True)
    restored_at = Column(DateTime,    nullable=True)
    restored_by = Column(String(100), nullable=True)

    __table_args__ = (Index("ix_recycle_batch_tbl", "batch_id", "table_name"),)


# ─── Bootstrap ────────────────────────────────────────────────────────────────
def _run_migrations():
    """
    Safe column-level migrations for SQLite (and MySQL).
    Each block checks whether the column already exists before running ALTER TABLE,
    so this is idempotent and safe to call on every startup.
    """
    from sqlalchemy import text as _text
    # Per-table additive column migrations (idempotent). New tables are created by
    # create_all(); this only ALTERs pre-existing tables that lack new columns.
    MIGRATIONS = {
        "transactions": [
            ("net_amount", "FLOAT"), ("override_note", "TEXT"),
            ("override_by", "VARCHAR(100)"), ("override_at", "DATETIME"),
            ("prev_recon_status", "VARCHAR(50)"),
            # Exception workflow (Tier 1)
            ("assigned_to", "VARCHAR(100)"), ("assigned_at", "DATETIME"),
            ("exception_reason", "VARCHAR(40)"),
            ("bank_account", "VARCHAR(40)"),
            # Read-only bank-statement narration (bank side only)
            ("bank_description", "TEXT"),
            # CSP (retailer) identity from the internal dump (internal side only)
            ("csp_code", "VARCHAR(40)"), ("csp_name", "VARCHAR(255)"),
            # Running statement balance (funds-position feature; display-only)
            ("balance", "FLOAT"),
        ],
        "audit_logs": [("action_type", "VARCHAR(10)"), ("previous_state", "TEXT")],
        "evalue_bank_txns": [
            ("recovery_status", "VARCHAR(20)"), ("recovery_note", "VARCHAR(300)"),
            ("override_by", "VARCHAR(100)"), ("override_at", "DATETIME"),
            ("prev_recon_status", "VARCHAR(30)"),
            # SRC disposition (parity with core-ledger assign-src)
            ("src_code", "VARCHAR(50)"), ("src_note", "VARCHAR(500)"),
        ],
        "evalue_wallet_loads": [
            ("override_by", "VARCHAR(100)"), ("override_at", "DATETIME"),
            ("prev_recon_status", "VARCHAR(30)"), ("bank_ref", "VARCHAR(120)"),
            ("src_code", "VARCHAR(50)"), ("src_note", "VARCHAR(500)"),
        ],
        # BBPS module tables gain SRC parity (added here so the ALTER runs on
        # already-deployed DBs — create_all only makes missing tables).
        "bbps_bank_txns": [
            ("src_code", "VARCHAR(50)"), ("src_note", "VARCHAR(500)"),
        ],
        "bbps_internal": [
            ("src_code", "VARCHAR(50)"), ("src_note", "VARCHAR(500)"),
        ],
        "api_keys": [("allowed_ips", "VARCHAR(500)")],
        "partner_configs": [("settlement_carry_days", "INTEGER")],
        "users": [("allowed_products", "TEXT")],
        # Per-rule side-pairing scope (Rajendra 2026-07-27). Existing rows ALTER-in
        # as NULL → read as 'bank_internal' everywhere (coalesced); backfilled tidy
        # in seed_match_rules so the whole column is non-NULL after first startup.
        "match_rules": [("scope", "VARCHAR(30)")],
        # roadmap 1.6: data-quality profile on the 1.4 ingestion ledger
        "ingestion_events": [("dq_profile", "TEXT")],
        # Developer Portal request workflow (additive governance columns)
        "portal_requests": [
            ("assignee", "VARCHAR(100)"), ("github_issue_url", "VARCHAR(500)"),
        ],
        # Opt-in "attach executive dashboard" flag on scheduled reports
        "report_subscriptions": [("include_dashboard", "BOOLEAN")],
    }

    with engine.connect() as conn:
        if _is_sqlite:
            for table, cols in MIGRATIONS.items():
                try:
                    existing = {r[1] for r in conn.execute(_text(f"PRAGMA table_info({table})")).fetchall()}
                except Exception:
                    existing = set()
                if not existing:
                    continue  # table not present yet (create_all makes it)
                for col, dtype in cols:
                    if col not in existing:
                        try:
                            conn.execute(_text(f"ALTER TABLE {table} ADD COLUMN {col} {dtype}"))
                        except Exception:
                            pass
            conn.commit()
        else:
            from sqlalchemy import text
            db_name = conn.execute(text("SELECT DATABASE()")).scalar()
            def _mysql_cols(table):
                res = conn.execute(text(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :tbl"
                ), {"db": db_name, "tbl": table})
                return {r[0] for r in res.fetchall()}
            for table, cols in MIGRATIONS.items():
                existing = _mysql_cols(table)
                if not existing:
                    continue
                for col, dtype in cols:
                    if col not in existing:
                        try:
                            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {dtype} NULL"))
                        except Exception:
                            pass
            conn.commit()


# Covering indexes that keep the analytics dashboard fast as the data grows (the group-by hot
# paths do index-only scans instead of full-table scans). create_all only indexes NEW tables, so
# existing DBs need an idempotent add. (name, table, columns-DDL).
_PERF_INDEXES = [
    ("ix_txn_cover",           "transactions",     "(row_type, recon_date, partner, side, recon_status, amount)"),
    ("ix_sbip02_cover",        "sbi_p02_results",  "(recon_date, match_status, bank_amount)"),
    ("ix_sbip03_date_status",  "sbi_p03_results",  "(recon_date, match_status)"),
]


def _ensure_perf_indexes():
    """Idempotently create the analytics covering indexes. Index adds never change behaviour;
    each is guarded by an existence check and any failure is swallowed (never blocks startup)."""
    try:
        with engine.connect() as conn:
            if _is_sqlite:
                for name, table, cols in _PERF_INDEXES:
                    try:
                        conn.execute(_text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} {cols}"))
                    except Exception:
                        pass
                conn.commit()
            else:
                from sqlalchemy import text
                db_name = conn.execute(text("SELECT DATABASE()")).scalar()
                for name, table, cols in _PERF_INDEXES:
                    try:
                        exists = conn.execute(text(
                            "SELECT 1 FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=:db "
                            "AND TABLE_NAME=:tbl AND INDEX_NAME=:idx LIMIT 1"),
                            {"db": db_name, "tbl": table, "idx": name}).scalar()
                        if not exists:
                            conn.execute(text(f"CREATE INDEX {name} ON {table} {cols}"))
                    except Exception:
                        pass
                conn.commit()
    except Exception:
        pass


def init_db():
    """Create all tables and run safe column migrations on startup."""
    Base.metadata.create_all(bind=engine)
    _run_migrations()
    _ensure_perf_indexes()


def seed_audit_read_grandfather(db):
    """One-time grandfather for roadmap item 1.2 (admin-gate audit READ).

    The /api/audit read endpoints are moving behind the ``audit_read``
    permission. To avoid 403-ing anyone who has open access TODAY, grant
    ``audit_read: true`` to every user that already exists the first time this
    runs. Users (and API keys) created afterwards get the app defaults, which do
    NOT include ``audit_read`` — that is the actual lock-down. Admins
    short-circuit in ``require_permission()`` and don't strictly need the flag.

    Guarded by a ``SystemSetting`` marker so it runs EXACTLY ONCE per database:
    it must never re-grant the permission to users created later, nor undo an
    admin who later revokes it. Runs in the actor-less startup session, so the
    config-audit listener (item 1.1) does not record these grants.
    """
    import json as _json
    MARKER = "audit_read_grandfathered_v1"
    if db.query(SystemSetting).filter(SystemSetting.key == MARKER).first():
        return  # already applied on this database
    for u in db.query(User).all():
        try:
            perms = _json.loads(u.permissions or "{}")
        except Exception:
            perms = {}
        if "audit_read" not in perms:
            perms["audit_read"] = True
            u.permissions = _json.dumps(perms)
    db.add(SystemSetting(key=MARKER, value="done", updated_by="system"))
    db.commit()


def seed_partner_configs(db):
    """
    Seed default PartnerConfig rows for all known partners.
    Safe to call on every startup — skips rows that already exist (matched by slug).
    """
    import json as _json
    defaults = [
        {"slug": "fino",      "display_name": "Fino Payments Bank",     "match_prefix": "FNO", "product": "dmt",       "source_value": "FINO",                  "has_bank_statement": True,  "has_internal_dump": True,  "sort_order": 1},
        {"slug": "airtel",    "display_name": "Airtel Payments Bank",   "match_prefix": "AIR", "product": "dmt",       "source_value": "AIRTEL",                "has_bank_statement": True,  "has_internal_dump": True,  "sort_order": 2},
        {"slug": "axis",      "display_name": "Axis Bank",              "match_prefix": "AXS", "product": "dmt",       "source_value": "Axis",                  "has_bank_statement": True,  "has_internal_dump": True,  "sort_order": 3},
        {"slug": "levin",     "display_name": "Levin",                  "match_prefix": "LVN", "product": "dmt",       "source_value": "Levin",                 "has_bank_statement": True,  "has_internal_dump": True,  "sort_order": 4},
        {"slug": "aeps",      "display_name": "AePS Cashout",           "match_prefix": "APS", "product": "aeps",      "source_value": None,                    "has_bank_statement": True,  "has_internal_dump": True,  "sort_order": 5},
        {"slug": "pg",        "display_name": "Accept Payment (PG)",    "match_prefix": "PGW", "product": "pg",        "source_value": None,                    "has_bank_statement": True,  "has_internal_dump": True,  "sort_order": 6},
        {"slug": "digikhata", "display_name": "Digikhata (PPI)",        "match_prefix": "DGK", "product": "digikhata", "source_value": "Digi Khata Load Wallet", "has_bank_statement": True,  "has_internal_dump": True,  "sort_order": 7},
        {"slug": "indonepal", "display_name": "Indo-Nepal",             "match_prefix": "INP", "product": "indonepal", "source_value": "Indo-Nepal",             "has_bank_statement": True,  "has_internal_dump": True,  "sort_order": 8},
        {"slug": "qr",        "display_name": "QR Collection",          "match_prefix": "QRC", "product": "qr",        "source_value": "QR Collection",          "has_bank_statement": True,  "has_internal_dump": True,  "sort_order": 9},
        {"slug": "sbi",       "display_name": "SBI Kiosk Banking",      "match_prefix": "SBI", "product": "kiosk",     "source_value": None,                     "has_bank_statement": True,  "has_internal_dump": True,  "sort_order": 10},
        {"slug": "evalue",    "display_name": "E-Value (Wallet Load)",  "match_prefix": "EV",  "product": "evalue",    "source_value": None,                     "has_bank_statement": True,  "has_internal_dump": True,  "sort_order": 11},
        {"slug": "bbps",      "display_name": "BBPS (Bill Pay/Recharge)","match_prefix": "BBP","product": "bbps",      "source_value": None,                     "has_bank_statement": True,  "has_internal_dump": True,  "sort_order": 12},
    ]
    for d in defaults:
        if not db.query(PartnerConfig).filter(PartnerConfig.slug == d["slug"]).first():
            db.add(PartnerConfig(**d))

    # One-time naming corrections (management naming decision, Jun 2026):
    #   Levin is a partner (Levin Fintech Pvt Ltd), not a bank.
    #   Fino's official brand is "Fino Payments Bank".
    #   The corridor is written "Indo-Nepal".
    _renames = {"levin": ("Levin Bank", "Levin"),
                "fino": ("Fino Bank", "Fino Payments Bank"),
                "indonepal": ("Indonepal", "Indo-Nepal")}
    for _slug, (_old, _new) in _renames.items():
        _row = db.query(PartnerConfig).filter(PartnerConfig.slug == _slug).first()
        if _row and _row.display_name == _old:
            _row.display_name = _new

    # Levin's bank-statement side was enabled once its sample statement arrived
    # (Jun 2026). Flip the flag on installs that were seeded before that.
    _lev = db.query(PartnerConfig).filter(PartnerConfig.slug == "levin").first()
    if _lev and not _lev.has_bank_statement:
        _lev.has_bank_statement = True

    db.commit()


def seed_bank_format_presets(db):
    """
    Seed BankFormatPreset rows from the hardcoded BANK_FORMAT_PRESETS dict.
    Safe to call on every startup — skips pairs that already exist.
    """
    import json as _json
    presets = [
        {
            "partner_slug": "fino", "side": "bank",
            "label": "Fino PTA Bank Statement",
            "signature_cols": _json.dumps(["description", "debits", "credits", "ending balance"]),
            "column_mapping": _json.dumps({"transaction_date": "Transaction Effective Date", "description": "Description"}),
            "special_config": _json.dumps({"debit_col": "Debits", "credit_col": "Credits"}),
        },
        {
            "partner_slug": "fino", "side": "internal",
            "label": "Fino Simplibank DMT Dump",
            "signature_cols": _json.dumps(["eko_trxn_id", "trackingnumber", "utrnumber", "debit_credit"]),
            "column_mapping": _json.dumps({"eko_tid": "eko_trxn_id", "tracking_number": "TrackingNumber", "utr_number": "UTRNUMBER", "transaction_date": "Transaction_Date"}),
            "special_config": _json.dumps({}),
        },
        {
            "partner_slug": "airtel", "side": "bank",
            "label": "Airtel Payments Bank Statement",
            "signature_cols": _json.dumps(["date", "time", "apb_txn_id", "partner_txn_id", "rrn", "transaction amount", "original amount", "transaction type", "description"]),
            "column_mapping": _json.dumps({"transaction_date": "Date", "description": "Description", "eko_tid": "Partner_txn_id", "tracking_number": "RRN", "amount": "Original Amount", "utr_number": "APB_TXN_id"}),
            "special_config": _json.dumps({"dr_cr_col": "Transaction Type"}),
        },
        {
            "partner_slug": "aeps", "side": "bank",
            "label": "AePS Fingpay Report",
            "signature_cols": _json.dumps(["merchant transaction id", "response rrn", "fingpay transaction id", "transaction amount", "requested timestamp"]),
            "column_mapping": _json.dumps({"eko_tid": "Merchant Transaction Id", "tracking_number": "Response Rrn", "amount": "Transaction Amount", "transaction_date": "Requested Timestamp", "status": "Status Message"}),
            "special_config": _json.dumps({}),
        },
        {
            "partner_slug": "aeps", "side": "internal",
            "label": "AePS Simplibank Dump",
            "signature_cols": _json.dumps(["eko_trxn_id", "trackingnumber", "typename", "business_vertical"]),
            "column_mapping": _json.dumps({"eko_tid": "eko_trxn_id", "tracking_number": "TrackingNumber", "amount": "Amount", "transaction_date": "Transaction_Date", "status": "Status"}),
            "special_config": _json.dumps({"typename_filter": "AePS Cashout"}),
        },
        {
            "partner_slug": "pg", "side": "bank",
            "label": "PG PayU Settlement Report",
            "signature_cols": _json.dumps(["merchant txn id", "merchant utr", "payu id", "amount", "net amount", "settlement date"]),
            "column_mapping": _json.dumps({"eko_tid": "Merchant Txn ID", "utr_number": "Merchant UTR", "amount": "Amount", "transaction_date": "AddedOn", "status": "Status"}),
            "special_config": _json.dumps({"net_amount_col": "Net Amount"}),
        },
        {
            "partner_slug": "pg", "side": "internal",
            "label": "PG Simplibank Dump",
            "signature_cols": _json.dumps(["eko_trxn_id", "trackingnumber", "typename", "business_vertical"]),
            "column_mapping": _json.dumps({"eko_tid": "eko_trxn_id", "amount": "Amount", "transaction_date": "Transaction_Date", "status": "Status"}),
            "special_config": _json.dumps({"typename_filter": "Accept Payment"}),
        },
        {
            "partner_slug": "axis", "side": "bank",
            "label": "Axis Bank Statement",
            "signature_cols": _json.dumps(["s.no", "transaction date (dd/mm/yyyy)", "particulars", "amount(inr)", "debit/credit", "balance(inr)"]),
            "column_mapping": _json.dumps({"transaction_date": "Transaction Date (dd/mm/yyyy)", "description": "Particulars"}),
            "special_config": _json.dumps({"debit_credit_col": "Debit/Credit", "amount_col": "Amount(INR)"}),
        },
        {
            "partner_slug": "axis", "side": "internal",
            "label": "Axis Simplibank Dump",
            "signature_cols": _json.dumps(["eko_trxn_id", "trackingnumber", "debit_credit", "source"]),
            "column_mapping": _json.dumps({"eko_tid": "eko_trxn_id", "tracking_number": "TrackingNumber", "utr_number": "UTRNUMBER", "transaction_date": "Transaction_Date", "status": "Status"}),
            "special_config": _json.dumps({"source_filter": "Axis"}),
        },
        {
            "partner_slug": "levin", "side": "internal",
            "label": "Levin Simplibank Dump",
            "signature_cols": _json.dumps(["eko_trxn_id", "trackingnumber", "debit_credit", "source"]),
            "column_mapping": _json.dumps({"eko_tid": "eko_trxn_id", "tracking_number": "TrackingNumber", "utr_number": "UTRNUMBER", "transaction_date": "Transaction_Date", "status": "Status"}),
            "special_config": _json.dumps({"source_filter": "Levin"}),
        },
        {
            "partner_slug": "levin", "side": "bank",
            "label": "Levin Bank Statement",
            "signature_cols": _json.dumps(["date & time", "tx id", "product", "amount", "mode", "status", "utr", "client_ref_id"]),
            "column_mapping": _json.dumps({"eko_tid": "Client_Ref_Id", "utr_number": "UTR", "amount": "Amount", "transaction_date": "Date & Time", "status": "Status"}),
            "special_config": _json.dumps({"eko_tid_strip_prefix": "EKOI"}),
        },
        {
            "partner_slug": "qr", "side": "bank",
            "label": "QR Transaction Report (Paypoint)",
            "signature_cols": _json.dumps(["orderid", "rrn", "amount", "net amount", "status", "transaction date"]),
            "column_mapping": _json.dumps({"eko_tid": "OrderId", "tracking_number": "RRN", "amount": "Amount", "transaction_date": "Transaction Date", "status": "Status"}),
            "special_config": _json.dumps({"net_amount_col": "Net Amount"}),
        },
        {
            "partner_slug": "qr", "side": "internal",
            "label": "QR Simplibank Dump",
            "signature_cols": _json.dumps(["eko_trxn_id", "trackingnumber", "typename", "business_vertical"]),
            "column_mapping": _json.dumps({"eko_tid": "eko_trxn_id", "tracking_number": "TrackingNumber", "amount": "Amount", "transaction_date": "Transaction_Date", "status": "Status"}),
            "special_config": _json.dumps({"typename_filter": "QR Collection"}),
        },
        {
            "partner_slug": "digikhata", "side": "bank",
            "label": "Digikhata Bank Report",
            "signature_cols": _json.dumps(["txn id", "tracking number", "amount", "status", "transaction date"]),
            "column_mapping": _json.dumps({"eko_tid": "Txn ID", "tracking_number": "Tracking Number", "amount": "Amount", "transaction_date": "Transaction Date", "status": "Status"}),
            "special_config": _json.dumps({}),
        },
        {
            "partner_slug": "digikhata", "side": "internal",
            "label": "Digikhata Simplibank Dump",
            "signature_cols": _json.dumps(["eko_trxn_id", "trackingnumber", "typename", "business_vertical"]),
            "column_mapping": _json.dumps({"eko_tid": "eko_trxn_id", "tracking_number": "TrackingNumber", "amount": "Amount", "transaction_date": "Transaction_Date", "status": "Status"}),
            "special_config": _json.dumps({"typename_filter": "Digikhata"}),
        },
        {
            "partner_slug": "indonepal", "side": "bank",
            "label": "Indonepal Bank Report",
            "signature_cols": _json.dumps(["txn id", "tracking number", "utr", "amount", "status", "transaction date"]),
            "column_mapping": _json.dumps({"eko_tid": "Txn ID", "tracking_number": "Tracking Number", "utr_number": "UTR", "amount": "Amount", "transaction_date": "Transaction Date", "status": "Status"}),
            "special_config": _json.dumps({}),
        },
        {
            "partner_slug": "indonepal", "side": "internal",
            "label": "Indonepal Simplibank Dump",
            "signature_cols": _json.dumps(["eko_trxn_id", "trackingnumber", "typename", "business_vertical"]),
            "column_mapping": _json.dumps({"eko_tid": "eko_trxn_id", "tracking_number": "TrackingNumber", "utr_number": "UTRNUMBER", "amount": "Amount", "transaction_date": "Transaction_Date", "status": "Status"}),
            "special_config": _json.dumps({"typename_filter": "Indo Nepal"}),
        },
    ]
    for p in presets:
        exists = db.query(BankFormatPreset).filter(
            BankFormatPreset.partner_slug == p["partner_slug"],
            BankFormatPreset.side == p["side"]
        ).first()
        if not exists:
            db.add(BankFormatPreset(**p))
    db.commit()


def migrate_sbi_p01_statuses(db):
    """One-time relabel of legacy P01 settlement statuses to the two-state model
    (matched / unmatched), effective 2026-07-27. Idempotent — after the first run no
    legacy value remains, so it is a no-op. run_p01 also recomputes each date it runs;
    this covers dates whose source files were cleared and can no longer be re-run, and
    guarantees NO consumer ever sees a stale CREDITED/PENDING/PARTIAL/EXCESS."""
    from models.database import SBIP01Result
    try:
        n1 = db.query(SBIP01Result).filter(SBIP01Result.status == "CREDITED").update(
            {"status": "matched"}, synchronize_session=False)
        n2 = db.query(SBIP01Result).filter(
            SBIP01Result.status.in_(["PENDING", "PARTIAL", "EXCESS"])).update(
            {"status": "unmatched"}, synchronize_session=False)
        if n1 or n2:
            db.commit()
    except Exception:
        db.rollback()


def seed_match_rules(db):
    """
    Seed default matching rules for all known partners into the MatchRule table.
    Safe to call every startup — skips partners that already have rules.
    """
    import json as _json
    # Single source of truth for default matching rules — imported from the engine's
    # DEFAULT_RULES so the DB seed and the engine fallback can never drift again (audit #4).
    # Lazy import (inside the function) avoids a circular import at module load.
    from core.matching_engine import DEFAULT_RULES as default_rules
    from models.database import MatchRule
    # Backfill: rows that pre-date the `scope` column ALTER-in as NULL → set them to
    # the historical behaviour. Idempotent (no NULLs remain after the first run).
    try:
        db.query(MatchRule).filter(MatchRule.scope.is_(None)).update(
            {"scope": "bank_internal"}, synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()
    for partner, rules in default_rules.items():
        existing = db.query(MatchRule).filter(MatchRule.partner == partner).first()
        if not existing:
            for r in rules:
                db.add(MatchRule(
                    name=r["name"],
                    partner=partner,
                    priority=r["priority"],
                    match_fields=_json.dumps(r["fields"]),
                    is_active=True,
                ))

    # Levin gained a bank-statement side (Jun 2026) that matches on UTR or the
    # trimmed Client_Ref_Id (= eko_tid). Existing installs already have Levin
    # rules, so add a "UTR Only" rule if it's missing (no rows are deleted).
    _lev_rules = db.query(MatchRule).filter(MatchRule.partner == "levin").all()
    if _lev_rules and not any(r.name == "UTR Only" for r in _lev_rules):
        _maxp = max((r.priority for r in _lev_rules), default=0)
        db.add(MatchRule(name="UTR Only", partner="levin", priority=_maxp + 1,
                         match_fields=_json.dumps(["utr_number"]), is_active=True))
    db.commit()


def seed_watch_folder_configs(db):
    """
    Create default WatchFolderConfig rows if they don't exist yet.
    Safe to call on every startup — skips rows that already exist.
    """
    defaults = [
        # ── DMT (existing) ────────────────────────────────────────────────────
        {"label": "Fino Bank Statement",    "partner": "fino",   "side": "bank",     "file_prefix": "FINO_BANK_"},
        {"label": "Airtel Bank Statement",  "partner": "airtel", "side": "bank",     "file_prefix": "AIRTEL_BANK_"},
        {"label": "Fino Internal Dump",     "partner": "fino",   "side": "internal", "file_prefix": "FINO_DUMP_"},
        {"label": "Airtel Internal Dump",   "partner": "airtel", "side": "internal", "file_prefix": "AIRTEL_DUMP_"},
        # ── AePS Cashout (new) ────────────────────────────────────────────────
        {"label": "AePS Fingpay Report",    "partner": "aeps",   "side": "bank",     "file_prefix": "AEPS_BANK_"},
        {"label": "AePS Simplibank Dump",   "partner": "aeps",   "side": "internal", "file_prefix": "AEPS_DUMP_"},
        # ── Accept Payment / PG (new) ─────────────────────────────────────────
        {"label": "PG PayU Report",         "partner": "pg",     "side": "bank",     "file_prefix": "PG_BANK_"},
        {"label": "PG Simplibank Dump",     "partner": "pg",     "side": "internal", "file_prefix": "PG_DUMP_"},
        # ── Axis Bank DMT ─────────────────────────────────────────────────────
        {"label": "Axis Bank Statement",          "partner": "axis",      "side": "bank",     "file_prefix": "AXIS_BANK_"},
        {"label": "Axis Internal Dump",           "partner": "axis",      "side": "internal", "file_prefix": "AXIS_DUMP_"},
        # ── Levin DMT ─────────────────────────────────────────────────────────
        {"label": "Levin Bank Statement",         "partner": "levin",     "side": "bank",     "file_prefix": "LEVIN_BANK_", "file_suffix": ".xlsx"},
        {"label": "Levin Internal Dump",          "partner": "levin",     "side": "internal", "file_prefix": "LEVIN_DUMP_"},
        # ── QR Collection ─────────────────────────────────────────────────────
        {"label": "QR Transaction Report",        "partner": "qr",        "side": "bank",     "file_prefix": "QR_BANK_",        "file_suffix": ".xlsx"},
        {"label": "QR Simplibank Dump",           "partner": "qr",        "side": "internal", "file_prefix": "QR_DUMP_",        "file_suffix": ".csv"},
        # ── Digikhata (PPI Wallet Load) ───────────────────────────────────────
        {"label": "Digikhata Bank Report",        "partner": "digikhata", "side": "bank",     "file_prefix": "DIGIKHATA_BANK_", "file_suffix": ".xlsx"},
        {"label": "Digikhata Simplibank Dump",    "partner": "digikhata", "side": "internal", "file_prefix": "DIGIKHATA_DUMP_", "file_suffix": ".csv"},
        # ── Indonepal ─────────────────────────────────────────────────────────
        {"label": "Indonepal Bank Report",        "partner": "indonepal", "side": "bank",     "file_prefix": "INDONEPAL_BANK_", "file_suffix": ".xlsx"},
        {"label": "Indonepal Simplibank Dump",    "partner": "indonepal", "side": "internal", "file_prefix": "INDONEPAL_DUMP_", "file_suffix": ".csv"},
        # ── SBI Kiosk Banking (P01–P04) ───────────────────────────────────────
        # Uses special 'sbi' partner; 'side' encodes the SBI file type so the
        # auto-upload trigger can route to the correct /api/sbi/upload/* endpoint.
        {"label": "SBI Bank Statement",          "partner": "sbi", "side": "bank_statement",  "file_prefix": "SBI_BANK_",    "file_suffix": ".xls"},
        {"label": "SBI KO Limits Config Report", "partner": "sbi", "side": "ko_limits",       "file_prefix": "KO_LIMITS_",   "file_suffix": ".xls"},
        {"label": "SBI Transaction Report",      "partner": "sbi", "side": "txn_report",       "file_prefix": "SBI_TXN_",     "file_suffix": ".xls"},
        {"label": "SBI KO Cash Holding Report",  "partner": "sbi", "side": "ko_cash_holding",  "file_prefix": "KO_CASH_",     "file_suffix": ".xls"},
        {"label": "SBI Limit Failures Report",   "partner": "sbi", "side": "limit_failures",   "file_prefix": "LIMIT_FAIL_",  "file_suffix": ".xls"},
        {"label": "SBI CSP Master Sheet",        "partner": "sbi", "side": "csp_master",        "file_prefix": "CSP_MASTER_",  "file_suffix": ".xlsx"},
        # ── BBPS (Bill Pay / Recharge) ────────────────────────────────────────
        # 'internal' = Simplibank dump; 'bank' = Moneyart/Levin operator statement
        # (provider auto-detected by the BBPS engine).
        {"label": "BBPS Simplibank Dump",        "partner": "bbps", "side": "internal",         "file_prefix": "BBPS_DUMP_",   "file_suffix": ".csv"},
        {"label": "BBPS Operator Statement",     "partner": "bbps", "side": "bank",             "file_prefix": "BBPS_OPER_",   "file_suffix": ".xlsx"},
        # ── E-Value (Wallet Load) ─────────────────────────────────────────────
        # Internal load-in dump covers all accounts in one file (auto-uploadable).
        # Bank statements are per-account, so they stay manual in the E-Value window.
        {"label": "E-Value Load-in Dump",        "partner": "evalue", "side": "internal",       "file_prefix": "EVALUE_DUMP_", "file_suffix": ".csv"},
    ]
    for d in defaults:
        # Use label as the uniqueness key so multiple entries per partner (e.g. Digikhata
        # bank + dump) can coexist without one blocking the other from being seeded.
        exists = db.query(WatchFolderConfig).filter(
            WatchFolderConfig.label == d["label"]
        ).first()
        if not exists:
            db.add(WatchFolderConfig(**d))
    db.commit()
