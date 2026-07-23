"""SQLAlchemy models — one table per ERP module. The `movements` table is the
immutable stock ledger used for history + as-of reporting."""
from sqlalchemy import (Column, Integer, BigInteger, String, Numeric, Boolean,
                        Date, DateTime, ForeignKey, Text, UniqueConstraint, func)
from sqlalchemy.orm import relationship
from .database import Base

class Branch(Base):
    __tablename__ = "branches"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
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
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
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
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    role = Column(String, nullable=False)
    email = Column(String)
    password_hash = Column(String, nullable=False)
    status = Column(String, default="active")
    # Identities provisioned for an employee's Telegram session cannot sign in
    # to the web app — they exist purely to carry that employee's RBAC.
    can_login = Column(Boolean, default=True)
    # A newly provisioned account must set its own password before it can do
    # anything: the temporary one is known to whoever created the account.
    must_change_password = Column(Boolean, default=False)
    employee_id = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    branches = relationship("UserBranch", cascade="all, delete-orphan", backref="user")

    @property
    def branch_names(self):
        return [b.branch for b in self.branches]

class UserBranch(Base):
    __tablename__ = "user_branches"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    branch = Column(String, ForeignKey("branches.name"), primary_key=True)

class Product(Base):
    __tablename__ = "products"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
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
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    sku = Column(String, ForeignKey("products.sku", ondelete="CASCADE"), primary_key=True)
    branch = Column(String, ForeignKey("branches.name"), primary_key=True)
    qty = Column(Integer, default=0)

class Movement(Base):
    __tablename__ = "movements"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    ref = Column(String); sku = Column(String, ForeignKey("products.sku"), index=True)
    branch = Column(String, index=True)
    type = Column(String)                 # receive|adjust|transfer_in|transfer_out|sale
    qty_before = Column(Integer); qty_change = Column(Integer); qty_after = Column(Integer)
    unit_cost = Column(Numeric(12, 2)); user_id = Column(String); notes = Column(Text)
    moved_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

class Ledger(Base):
    __tablename__ = "ledger"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
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
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
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
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
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
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    id = Column(String, primary_key=True); vendor = Column(String); branch = Column(String, index=True)
    amount = Column(Numeric(12, 2)); status = Column(String, default="pending_approval")
    purchase_date = Column(Date, server_default=func.current_date())

class Transfer(Base):
    __tablename__ = "transfers"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    id = Column(String, primary_key=True); sku = Column(String)
    from_branch = Column(String); to_branch = Column(String)
    qty = Column(Integer); status = Column(String, default="pending")

class Customer(Base):
    # Wave B (B-B) — customers: business number `id` is per-company. EXPAND phase
    # keeps `id` as the PK and adds the surrogate `row_id` (DB-side) plus a
    # composite unique (company_id, id). CONTRACT (B-B-C1-contract) moves the PK to
    # the surrogate `row_id`, leaving `id` a tenant-scoped visible business number.
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("company_id", "id", name="uq_customers_company_id"),)
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    id = Column(String, primary_key=True); name = Column(String); balance = Column(Numeric(12, 2), default=0)

class Supplier(Base):
    __tablename__ = "suppliers"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    id = Column(String, primary_key=True); name = Column(String); balance = Column(Numeric(12, 2), default=0)

class Approval(Base):
    __tablename__ = "approvals"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    id = Column(String, primary_key=True); kind = Column(String); ref = Column(String)
    branch = Column(String, index=True); amount = Column(Numeric(12, 2))
    requested_by = Column(String); summary = Column(String); status = Column(String, default="pending")
    decided_by = Column(String); comment = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class ClockEvent(Base):
    __tablename__ = "clock_events"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    employee = Column(String); branch = Column(String); direction = Column(String)
    at_ts = Column(DateTime(timezone=True), server_default=func.now())

class AuditLog(Base):
    __tablename__ = "audit_log"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
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
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
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
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    code = Column(String, primary_key=True); user_id = Column(String)
    expires_at = Column(DateTime(timezone=True)); used = Column(Boolean, default=False)
    employee_id = Column(String)     # the employee this invitation is for
    created_by = Column(String)      # who minted it


class ValidationRun(Base):
    """Financial Control Center audit history. This is the ONLY table the
    control module writes to — every validation check itself is read-only."""
    __tablename__ = "validation_runs"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
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
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
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
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
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
    # Wave B (B-A CONTRACT): key is per-company. The primary key is the composite
    # (company_id, key); the legacy global `key` PK and its interim composite
    # unique index (B-A1) have been retired. Childless table → composite PK, no
    # surrogate. company_id is part of the PK, hence NOT NULL.
    __tablename__ = "company_settings"
    company_id = Column(Integer, primary_key=True, nullable=False, server_default="1")  # tenant owner
    key = Column(String, primary_key=True)
    value = Column(Text)
    updated_by = Column(String)
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


# =========================================================================
# TEAM CHAT — normalized schema. Near-real-time via short polling (no
# WebSocket infra on the current host); attachments deferred (no object store).
# =========================================================================
class ChatRoom(Base):
    __tablename__ = "chat_rooms"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    kind = Column(String, default="group")     # company|branch|department|management|private|group
    name = Column(String)
    branch = Column(String, index=True)        # set for branch rooms
    department = Column(String)                # set for department rooms
    created_by = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    archived = Column(Boolean, default=False)


class ChatMember(Base):
    __tablename__ = "chat_members"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    room_id = Column(BigInteger, index=True)
    user_id = Column(String, index=True)
    role = Column(String, default="member")    # member | admin
    last_read_id = Column(BigInteger, default=0)
    muted = Column(Boolean, default=False)
    joined_at = Column(DateTime(timezone=True), server_default=func.now())


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    room_id = Column(BigInteger, index=True)
    user_id = Column(String, index=True)
    body = Column(Text)
    kind = Column(String, default="text")      # text | erp_card | system | alert
    erp_ref = Column(String)                   # JSON: {type, id, label, view}
    reply_to = Column(BigInteger)
    pinned = Column(Boolean, default=False)
    edited = Column(Boolean, default=False)
    deleted = Column(Boolean, default=False)
    mentions = Column(String)                  # JSON list of user ids / role / branch tokens
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    edited_at = Column(DateTime(timezone=True))


class ChatReaction(Base):
    __tablename__ = "chat_reactions"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    message_id = Column(BigInteger, index=True)
    user_id = Column(String)
    emoji = Column(String)


class ChatPresence(Base):
    __tablename__ = "chat_presence"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    user_id = Column(String, primary_key=True)
    last_seen = Column(DateTime(timezone=True))
    typing_room = Column(BigInteger)
    typing_at = Column(DateTime(timezone=True))


class ChatTask(Base):
    __tablename__ = "chat_tasks"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    room_id = Column(BigInteger, index=True)
    message_id = Column(BigInteger)
    title = Column(Text)
    assignee = Column(String, index=True)
    priority = Column(String, default="normal")   # low|normal|high|urgent
    due_date = Column(Date)
    status = Column(String, default="open")        # open|in_progress|done
    percent = Column(Integer, default=0)
    created_by = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ChatAnnouncement(Base):
    __tablename__ = "chat_announcements"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    scope = Column(String, default="company")      # company | branch
    branch = Column(String)
    title = Column(String)
    body = Column(Text)
    created_by = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    active = Column(Boolean, default=True)


# =========================================================================
# RECURRING TELEGRAM REMINDERS — interval-based nudges to enter business data.
# Reuses the existing Telegram integration + company timezone. Like the
# scheduled reports, the entire schedule lives in the database (one settings
# row), so the worker holds no state and a restart/redeploy loses nothing.
# =========================================================================
class ReminderSetting(Base):
    """Single-row configuration for the recurring reminder (id is always 1).

    The scheduler reads this every tick; `next_run_at` (UTC) is the source of
    truth for *when* the next reminder fires, advanced atomically on each claim
    so two worker instances can never double-send.
    """
    __tablename__ = "reminder_settings"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    id = Column(Integer, primary_key=True, default=1)
    enabled = Column(Boolean, default=False)
    interval_hours = Column(Integer, default=12)          # every N hours
    message = Column(Text)                                # editable reminder body
    active_start_hour = Column(Integer, default=8)        # local hour, inclusive
    active_end_hour = Column(Integer, default=22)         # local hour, inclusive
    paused_days = Column(Text)                            # JSON list, 0=Mon..6=Sun
    recipient_mode = Column(String, default="all")        # all | selected
    recipient_ids = Column(Text)                          # JSON list of tg_id (when selected)
    next_run_at = Column(DateTime(timezone=True))         # UTC; when the next reminder is due
    last_run_at = Column(DateTime(timezone=True))         # UTC; last time a batch was claimed
    updated_by = Column(String)
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


class ReminderDelivery(Base):
    """One row per recipient per reminder send — the audit + idempotency ledger.

    idem_key is UNIQUE (reminder|<run-iso>|<tg_id>, or manual|<stamp>|<tg_id>),
    so a retry or a second worker instance can never log or send twice for the
    same slot and recipient. A schedule-level 'skipped' row (outside active
    hours / paused day) is recorded with tg_id '-'.
    """
    __tablename__ = "reminder_deliveries"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    idem_key = Column(String, unique=True, index=True)
    run_at = Column(DateTime(timezone=True), index=True)   # scheduled/queued time (UTC)
    kind = Column(String, default="scheduled")             # scheduled | manual | skipped
    tg_id = Column(String, index=True)
    recipient = Column(String)                             # employee/display name
    message = Column(Text)                                 # exact body queued (manual sends)
    status = Column(String, default="queued")              # queued|sent|failed|skipped
    error = Column(Text)
    message_id = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# =========================================================================
# EMPLOYEE WORK SCHEDULE — normalized weekly scheduling with a calendar and
# per-employee Telegram delivery. Reuses the existing Employees module,
# TelegramLink (account↔employee) and company timezone. Additive: the legacy
# Employee.sched_* fields (used by attendance) are left untouched.
# =========================================================================
class EmployeeSchedule(Base):
    """One shift row per employee per calendar date — the calendar's source data.

    A day with `is_off` (or simply no row) reads as OFF. `week_start` (the Monday
    of the shift's ISO week) groups a week for publishing and delivery.
    """
    __tablename__ = "employee_schedules"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    employee_id = Column(String, index=True)
    branch = Column(String, index=True)
    work_date = Column(Date, index=True)
    week_start = Column(Date, index=True)                  # Monday of work_date's week
    start_time = Column(String, default="09:00")           # HH:MM local
    end_time = Column(String, default="17:00")             # HH:MM local
    break_minutes = Column(Integer, default=0)
    notes = Column(Text)
    is_off = Column(Boolean, default=False)
    published = Column(Boolean, default=False)
    template_id = Column(BigInteger)
    created_by = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


class ScheduleTemplate(Base):
    """A reusable recurring pattern (Mon–Fri, weekends, alternate weeks, custom).

    `weekdays` is a JSON list of 0=Mon..6=Sun. `recurrence` distinguishes weekly
    from alternate-week expansion; `every_other` marks the alternate cadence.
    """
    __tablename__ = "schedule_templates"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    name = Column(String)
    recurrence = Column(String, default="weekly")          # weekly|weekdays|weekends|alternate|custom
    weekdays = Column(Text)                                 # JSON list of int 0..6
    every_other = Column(Boolean, default=False)           # alternate-week cadence
    start_time = Column(String, default="09:00")
    end_time = Column(String, default="17:00")
    break_minutes = Column(Integer, default=0)
    notes = Column(Text)
    created_by = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ScheduleException(Base):
    """A one-off override for a specific date (time-off, swap, modified hours)."""
    __tablename__ = "schedule_exceptions"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    employee_id = Column(String, index=True)
    work_date = Column(Date, index=True)
    kind = Column(String, default="off")                   # off | modified
    start_time = Column(String)
    end_time = Column(String)
    break_minutes = Column(Integer, default=0)
    reason = Column(Text)
    created_by = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class TelegramDeliveryLog(Base):
    """Schedule-delivery audit + idempotency ledger.

    idem_key is UNIQUE (e.g. publish|<week>|<tg_id>|<stamp> or weekly|<week>|<tg_id>),
    so a retry, a redeploy or a second worker instance can never send or log the
    same schedule twice for the same recipient.
    """
    __tablename__ = "telegram_delivery_log"
    company_id = Column(Integer, index=True, nullable=True, server_default="1")  # tenant owner; backfilled to Company #1
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    idem_key = Column(String, unique=True, index=True)
    employee_id = Column(String, index=True)
    tg_id = Column(String, index=True)
    recipient = Column(String)
    week_start = Column(Date, index=True)
    kind = Column(String, default="publish")               # publish|update|weekly|manual|copy
    status = Column(String, default="queued")              # queued|sent|failed|skipped
    message = Column(Text)                                 # exact body to send (pre-rendered)
    error = Column(Text)
    message_id = Column(String)
    created_by = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# =========================================================================
# PFS PLATFORM — multi-tenant SaaS foundation (Phase 0).
# These tables are ADDITIVE and platform-level. No existing tenant table is
# touched in this phase. The live SmokeStack business becomes Company #1.
# Tenancy (company_id on tenant tables) is introduced in Phase 1.
# =========================================================================
class PlatformUser(Base):
    """A Super Admin of the PFS Control Center — separate from tenant `users`.
    Belongs to no company; can never be used on tenant endpoints."""
    __tablename__ = "platform_users"
    id = Column(String, primary_key=True)                  # e.g. "SA-root"
    username = Column(String, unique=True, index=True)
    name = Column(String)
    password_hash = Column(String)
    active = Column(Boolean, default=True)
    must_change_password = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_login = Column(DateTime(timezone=True))


class Application(Base):
    """A registered ERP application type (smoke_shop, retail, restaurant, …).
    Seeded from the code manifest so new ERPs register without core changes."""
    __tablename__ = "applications"
    key = Column(String, primary_key=True)                 # smoke_shop
    name = Column(String)
    industry = Column(String)
    description = Column(Text)
    active = Column(Boolean, default=True)                  # is the app live yet?
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Company(Base):
    """A tenant. Company #1 is the existing SmokeStack business (created by the
    Phase 0 seed). status drives lifecycle: active|suspended|archived|deleted."""
    __tablename__ = "companies"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String)
    slug = Column(String, unique=True, index=True)
    industry = Column(String)
    application_key = Column(String, default="smoke_shop")
    owner_user_id = Column(String)                         # tenant user id of the owner
    status = Column(String, default="active")              # active|suspended|archived|deleted
    version = Column(String)                               # platform version the company is on
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    suspended_at = Column(DateTime(timezone=True))
    archived_at = Column(DateTime(timezone=True))
    deleted_at = Column(DateTime(timezone=True))
    last_activity = Column(DateTime(timezone=True))


class Module(Base):
    """A capability that can be enabled per company. Seeded from the manifest.
    `depends_on` is a JSON list of other module keys (dependency graph)."""
    __tablename__ = "modules"
    key = Column(String, primary_key=True)                 # payroll, inventory, …
    name = Column(String)
    category = Column(String)                              # Payroll, Inventory, Core Platform …
    application_key = Column(String, default="core")       # core = shared across apps
    depends_on = Column(Text)                              # JSON list of module keys
    default_enabled = Column(Boolean, default=True)
    is_beta = Column(Boolean, default=False)
    version = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CompanyModule(Base):
    """Per-company enable state for a module. Absent row = manifest default.
    `source` records whether the value came from a global rollout or a local
    (per-company) override."""
    __tablename__ = "company_modules"
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    company_id = Column(Integer, index=True)
    module_key = Column(String, index=True)
    enabled = Column(Boolean, default=True)
    # Richer per-company module lifecycle (enforced server-side by app.policy):
    # enabled | disabled | hidden | maintenance | deprecated.
    state = Column(String, default="enabled")
    source = Column(String, default="global")              # global | local
    updated_by = Column(String)
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


class Subscription(Base):
    """A company's subscription. Gateway columns are present but unused for now
    (payment integration is a later phase)."""
    __tablename__ = "subscriptions"
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    company_id = Column(Integer, index=True)
    plan = Column(String, default="trial")                 # trial|monthly|quarterly|yearly|lifetime
    status = Column(String, default="active")              # active|trial|expired|suspended
    trial_ends = Column(Date)
    period_start = Column(Date)
    period_end = Column(Date)
    gateway = Column(String)                               # future: stripe|paddle|…
    gateway_customer_id = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


class PlatformAudit(Base):
    """Every Super Admin action — the Control Center's audit trail. Separate
    from the tenant AuditLog so platform actions can never be confused with a
    company's own activity."""
    __tablename__ = "platform_audit"
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    super_admin_id = Column(String, index=True)
    action = Column(String)                                # login_as, reset_password, module_enabled, …
    entity = Column(String)
    ref = Column(String)
    company_id = Column(Integer, index=True)
    detail = Column(Text)
    prev_value = Column(Text)
    new_value = Column(Text)
    ip = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class FeatureFlag(Base):
    """A feature toggle, independent of modules. Resolved most-specific-first by
    app.policy: user > company > subscription > industry > application > platform.
    Platform-owned (company_id is a pointer via scope_ref, not tenant ownership)."""
    __tablename__ = "feature_flags"
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    key = Column(String, index=True)
    scope = Column(String, default="platform")   # platform|application|company|industry|subscription|user
    scope_ref = Column(String)                    # e.g. company id / user id / app key / industry
    enabled = Column(Boolean, default=True)
    description = Column(Text)
    updated_by = Column(String)
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


class PolicyOverride(Base):
    """A temporary, auto-expiring, audited PFS emergency override that can ALLOW a
    normally-blocked action for one company. Never bypasses financial integrity."""
    __tablename__ = "policy_overrides"
    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    company_id = Column(Integer, index=True)
    action = Column(String)                       # read|write|export|jobs|all
    allow = Column(Boolean, default=True)
    reason = Column(Text)
    created_by = Column(String)                   # super_admin_id
    active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True))
