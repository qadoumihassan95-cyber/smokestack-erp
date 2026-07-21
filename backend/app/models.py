"""SQLAlchemy models — one table per ERP module. The `movements` table is the
immutable stock ledger used for history + as-of reporting."""
from sqlalchemy import (Column, Integer, BigInteger, String, Numeric, Boolean,
                        Date, DateTime, ForeignKey, Text, func)
from sqlalchemy.orm import relationship
from .database import Base

class Branch(Base):
    __tablename__ = "branches"
    name = Column(String, primary_key=True)
    # attendance geofence settings
    lat = Column(Numeric(10, 6)); lng = Column(Numeric(10, 6))
    radius_m = Column(Integer, default=150)
    timezone = Column(String, default="UTC")
    loc_verify = Column(Boolean, default=True)
    grace_min = Column(Integer, default=10)
    allow_override = Column(Boolean, default=True)
    attendance_active = Column(Boolean, default=True)


class Attendance(Base):
    __tablename__ = "attendance"
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    user_id = Column(String, index=True)
    employee_id = Column(String)
    employee_name = Column(String)
    tg_id = Column(String)
    branch = Column(String, index=True)
    clock_in_at = Column(DateTime(timezone=True))
    ci_lat = Column(Numeric(10, 6)); ci_lng = Column(Numeric(10, 6)); ci_dist = Column(Integer)
    clock_out_at = Column(DateTime(timezone=True))
    co_lat = Column(Numeric(10, 6)); co_lng = Column(Numeric(10, 6)); co_dist = Column(Integer)
    status = Column(String, default="active")        # active | completed | pending | rejected
    approval = Column(String, default="none")        # none | pending | approved | rejected
    approver = Column(String); approved_at = Column(DateTime(timezone=True)); reason = Column(String)
    late = Column(Boolean, default=False); worked_minutes = Column(Integer)
    source = Column(String, default="TELEGRAM")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    role = Column(String, nullable=False)
    email = Column(String)
    password_hash = Column(String, nullable=False)
    status = Column(String, default="active")
    # Identities provisioned for an employee's Telegram session cannot sign in
    # to the web app — they exist purely to carry that employee's RBAC.
    can_login = Column(Boolean, default=True)
    employee_id = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    branches = relationship("UserBranch", cascade="all, delete-orphan", backref="user")

    @property
    def branch_names(self):
        return [b.branch for b in self.branches]

class UserBranch(Base):
    __tablename__ = "user_branches"
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    branch = Column(String, ForeignKey("branches.name"), primary_key=True)

class Product(Base):
    __tablename__ = "products"
    sku = Column(String, primary_key=True)
    barcode = Column(String, index=True)
    name = Column(String, nullable=False)
    category = Column(String); brand = Column(String); supplier = Column(String)
    cost = Column(Numeric(12, 2), default=0)
    price = Column(Numeric(12, 2), default=0)
    min_level = Column(Integer, default=0)
    uom = Column(String, default="unit")
    shelf = Column(String)
    status = Column(String, default="active")

class Stock(Base):
    __tablename__ = "stock"
    sku = Column(String, ForeignKey("products.sku", ondelete="CASCADE"), primary_key=True)
    branch = Column(String, ForeignKey("branches.name"), primary_key=True)
    qty = Column(Integer, default=0)

class Movement(Base):
    __tablename__ = "movements"
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    ref = Column(String); sku = Column(String, ForeignKey("products.sku"), index=True)
    branch = Column(String, index=True)
    type = Column(String)                 # receive|adjust|transfer_in|transfer_out|sale
    qty_before = Column(Integer); qty_change = Column(Integer); qty_after = Column(Integer)
    unit_cost = Column(Numeric(12, 2)); user_id = Column(String); notes = Column(Text)
    moved_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

class Ledger(Base):
    __tablename__ = "ledger"
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    branch = Column(String, index=True)
    type = Column(String)                 # sale|expense|purchase|payroll|deposit
    amount = Column(Numeric(12, 2)); tax = Column(Numeric(12, 2), default=0)
    account = Column(String); category = Column(String); vendor = Column(String)
    employee = Column(String); product = Column(String); memo = Column(String)
    custom_description = Column(String)   # free-text detail when category == "Other"
    entry_date = Column(Date, server_default=func.current_date(), index=True)
    created_by = Column(String); created_at = Column(DateTime(timezone=True), server_default=func.now())

class Employee(Base):
    __tablename__ = "employees"
    id = Column(String, primary_key=True); name = Column(String, nullable=False)
    branch = Column(String, index=True); title = Column(String)
    pay_type = Column(String, default="salary")
    salary = Column(Numeric(12, 2), default=0); hourly_rate = Column(Numeric(12, 2), default=0)
    active = Column(Boolean, default=True)
    sched_start = Column(String, default="09:00")   # scheduled shift start HH:MM (branch tz)
    sched_end = Column(String, default="17:00")     # scheduled shift end HH:MM
    sched_days = Column(String, default="Mon-Sat")  # working days label
    # --- Telegram / session identity (additive) ---
    role = Column(String, default="employee")   # RBAC role this employee acts with
    user_id = Column(String)                    # login identity provisioned for this employee
    tg_perms = Column(Text)                     # JSON {capability: bool} — owner overrides
    created_by = Column(String); created_at = Column(DateTime(timezone=True), server_default=func.now())


class License(Base):
    __tablename__ = "licenses"
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    doc_type = Column(String)              # business_license | tobacco_license | sales_tax_permit | ...
    branch = Column(String, index=True)
    doc_number = Column(String)
    authority = Column(String)             # issuing authority
    issue_date = Column(Date)
    expiry_date = Column(Date, index=True)
    status = Column(String, default="active")   # active | expiring | expired | archived
    responsible = Column(String)           # responsible employee/name
    notes = Column(Text)
    attachment = Column(String)            # filename / URL of the uploaded PDF or image
    created_by = Column(String); created_at = Column(DateTime(timezone=True), server_default=func.now())

class Purchase(Base):
    __tablename__ = "purchases"
    id = Column(String, primary_key=True); vendor = Column(String); branch = Column(String, index=True)
    amount = Column(Numeric(12, 2)); status = Column(String, default="pending_approval")
    purchase_date = Column(Date, server_default=func.current_date())

class Transfer(Base):
    __tablename__ = "transfers"
    id = Column(String, primary_key=True); sku = Column(String)
    from_branch = Column(String); to_branch = Column(String)
    qty = Column(Integer); status = Column(String, default="pending")

class Customer(Base):
    __tablename__ = "customers"
    id = Column(String, primary_key=True); name = Column(String); balance = Column(Numeric(12, 2), default=0)

class Supplier(Base):
    __tablename__ = "suppliers"
    id = Column(String, primary_key=True); name = Column(String); balance = Column(Numeric(12, 2), default=0)

class Approval(Base):
    __tablename__ = "approvals"
    id = Column(String, primary_key=True); kind = Column(String); ref = Column(String)
    branch = Column(String, index=True); amount = Column(Numeric(12, 2))
    requested_by = Column(String); summary = Column(String); status = Column(String, default="pending")
    decided_by = Column(String); comment = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class ClockEvent(Base):
    __tablename__ = "clock_events"
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    employee = Column(String); branch = Column(String); direction = Column(String)
    at_ts = Column(DateTime(timezone=True), server_default=func.now())

class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    source = Column(String, default="WEB")
    tg_id = Column(String); user_id = Column(String)
    action = Column(String); entity = Column(String); ref = Column(String)
    detail = Column(Text); result = Column(String, default="ok")
    # --- richer Telegram audit context (additive) ---
    tg_username = Column(String); branch = Column(String)
    role = Column(String); ip = Column(String)

class TelegramLink(Base):
    __tablename__ = "telegram_links"
    tg_id = Column(String, primary_key=True); user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    username = Column(String); device = Column(String)
    linked_at = Column(DateTime(timezone=True), server_default=func.now())
    last_activity = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True))
    prefs = Column(Text)   # JSON string: notification toggles, quiet hours, language, default branch, timezone
    # --- enterprise multi-user fields (additive; existing rows default to active) ---
    status = Column(String, default="active")   # active | disabled
    employee_id = Column(String)                # the Employee this account represents
    linked_by = Column(String)                  # who issued the link code
    disabled_at = Column(DateTime(timezone=True))
    disabled_by = Column(String)

class LinkCode(Base):
    """An invitation to link ONE employee's Telegram account.

    user_id is the session identity the redeeming Telegram account will act as.
    employee_id is who the invitation was minted for — this is what makes the
    system multi-employee: previously the code carried only the signed-in user,
    so every code an owner generated pointed back at the owner."""
    __tablename__ = "link_codes"
    code = Column(String, primary_key=True); user_id = Column(String)
    expires_at = Column(DateTime(timezone=True)); used = Column(Boolean, default=False)
    employee_id = Column(String)     # the employee this invitation is for
    created_by = Column(String)      # who minted it


class ValidationRun(Base):
    """Financial Control Center audit history. This is the ONLY table the
    control module writes to — every validation check itself is read-only."""
    __tablename__ = "validation_runs"
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    ts = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    user_id = Column(String)
    score = Column(Numeric(5, 2))
    passed = Column(Integer, default=0)
    warnings = Column(Integer, default=0)
    errors = Column(Integer, default=0)
    critical = Column(Integer, default=0)
    duration_ms = Column(Integer)
    modules = Column(String)     # comma-separated modules that reported an issue
    severity = Column(String)    # worst severity in the run: ok|warning|error|critical
    report = Column(Text)        # full JSON report


class ReportRecipient(Base):
    """Who receives the scheduled Telegram reports, and how.

    Keyed by the Telegram account. Branch scope here can only NARROW the
    employee's ERP scope — it is intersected with it at send time, so a branch
    manager can never be configured into another branch's data.
    """
    __tablename__ = "report_recipients"
    tg_id = Column(String, primary_key=True)
    enabled = Column(Boolean, default=True)
    morning = Column(Boolean, default=True)
    evening = Column(Boolean, default=True)
    all_branches = Column(Boolean, default=True)   # send the combined report
    per_branch = Column(Boolean, default=True)     # send one report per branch
    branches = Column(Text)                        # JSON list; null = full ERP scope
    language = Column(String, default="en")
    include_pdf = Column(Boolean, default=False)
    urgent_alerts = Column(Boolean, default=True)
    updated_by = Column(String)
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


class ReportDelivery(Base):
    """Delivery log + the idempotency ledger that makes the scheduler safe.

    idem_key is UNIQUE. A worker claims a delivery by INSERTing the key; a second
    instance (or a restarted one) hits the constraint and skips, so the same
    report can never be sent twice for the same business date and slot.
    """
    __tablename__ = "report_deliveries"
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    idem_key = Column(String, unique=True, index=True)
    report_type = Column(String)          # morning | evening | manual | test
    business_date = Column(Date, index=True)
    scheduled_for = Column(DateTime(timezone=True))
    sent_at = Column(DateTime(timezone=True))
    recipient = Column(String)            # employee name
    tg_id = Column(String, index=True)
    branch_scope = Column(String)
    status = Column(String, default="pending")   # pending|processing|sent|partial|failed|skipped
    retries = Column(Integer, default=0)
    error = Column(Text)
    message_ids = Column(String)
    pdf_status = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CompanySetting(Base):
    """Company-wide configuration as key/value rows.

    Deliberately a table rather than a column on some other entity: settings are
    read on every scheduler tick and must survive restarts, and new settings must
    not require a schema change.
    """
    __tablename__ = "company_settings"
    key = Column(String, primary_key=True)
    value = Column(Text)
    updated_by = Column(String)
    updated_at = Column(DateTime(timezone=True), server_default=func.now())
