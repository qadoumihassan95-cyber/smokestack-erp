"""SQLAlchemy models — one table per ERP module. The `movements` table is the
immutable stock ledger used for history + as-of reporting."""
from sqlalchemy import (Column, Integer, BigInteger, String, Numeric, Boolean,
                        Date, DateTime, ForeignKey, Text, func)
from sqlalchemy.orm import relationship
from .database import Base

class Branch(Base):
    __tablename__ = "branches"
    name = Column(String, primary_key=True)

class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    role = Column(String, nullable=False)
    email = Column(String)
    password_hash = Column(String, nullable=False)
    status = Column(String, default="active")
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
    entry_date = Column(Date, server_default=func.current_date(), index=True)
    created_by = Column(String); created_at = Column(DateTime(timezone=True), server_default=func.now())

class Employee(Base):
    __tablename__ = "employees"
    id = Column(String, primary_key=True); name = Column(String, nullable=False)
    branch = Column(String, index=True); title = Column(String)
    pay_type = Column(String, default="salary")
    salary = Column(Numeric(12, 2), default=0); hourly_rate = Column(Numeric(12, 2), default=0)
    active = Column(Boolean, default=True)
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

class TelegramLink(Base):
    __tablename__ = "telegram_links"
    tg_id = Column(String, primary_key=True); user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    username = Column(String); device = Column(String)
    linked_at = Column(DateTime(timezone=True), server_default=func.now())
    last_activity = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True))

class LinkCode(Base):
    __tablename__ = "link_codes"
    code = Column(String, primary_key=True); user_id = Column(String)
    expires_at = Column(DateTime(timezone=True)); used = Column(Boolean, default=False)
