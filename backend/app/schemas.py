"""Pydantic request/response models."""
from pydantic import BaseModel
from typing import Optional, List
from datetime import date

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict

class LoginIn(BaseModel):
    username: str
    password: str

class ProductIn(BaseModel):
    sku: str
    name: str
    barcode: Optional[str] = None
    category: Optional[str] = None
    brand: Optional[str] = None
    supplier: Optional[str] = None
    cost: float = 0
    price: float = 0
    min_level: int = 0
    uom: str = "unit"
    shelf: Optional[str] = None

class ProductUpdate(BaseModel):
    """Partial update — every field optional so a PATCH can send just what changed."""
    name: Optional[str] = None
    barcode: Optional[str] = None
    category: Optional[str] = None
    brand: Optional[str] = None
    supplier: Optional[str] = None
    cost: Optional[float] = None
    price: Optional[float] = None
    min_level: Optional[int] = None
    uom: Optional[str] = None
    shelf: Optional[str] = None

class StockOp(BaseModel):
    sku: str
    branch: str
    qty: int
    reason: Optional[str] = None
    unit_cost: Optional[float] = None   # optional cost override on receive (else product cost)

class TransferIn(BaseModel):
    sku: str
    from_branch: str
    to_branch: str
    qty: int

class ExpenseIn(BaseModel):
    branch: str
    category: str
    amount: float
    account: Optional[str] = "Cash"
    memo: Optional[str] = None
    custom_description: Optional[str] = None   # required by the API when category == "Other"

class PurchaseIn(BaseModel):
    vendor: str
    branch: str
    amount: float

class SaleIn(BaseModel):
    branch: str
    amount: float
    tax: float = 0
    account: Optional[str] = "Cash"
    product: Optional[str] = None
    employee: Optional[str] = None

class EmployeeIn(BaseModel):
    id: str
    name: str
    branch: str
    title: Optional[str] = "Staff"
    pay_type: str = "salary"
    salary: float = 0
    hourly_rate: float = 0
    sched_start: Optional[str] = "09:00"
    sched_end: Optional[str] = "17:00"
    sched_days: Optional[str] = "Mon-Sat"

class EmployeeUpdate(BaseModel):
    """Partial update — id comes from the URL path, all body fields optional."""
    name: Optional[str] = None
    branch: Optional[str] = None
    title: Optional[str] = None
    pay_type: Optional[str] = None
    salary: Optional[float] = None
    hourly_rate: Optional[float] = None
    sched_start: Optional[str] = None
    sched_end: Optional[str] = None
    sched_days: Optional[str] = None

class LicenseIn(BaseModel):
    name: str
    doc_type: Optional[str] = "other"
    branch: Optional[str] = None
    doc_number: Optional[str] = None
    authority: Optional[str] = None
    issue_date: Optional[str] = None
    expiry_date: Optional[str] = None
    responsible: Optional[str] = None
    notes: Optional[str] = None
    attachment: Optional[str] = None

class ApprovalDecision(BaseModel):
    comment: Optional[str] = ""

class ClockIn(BaseModel):
    employee: str
    branch: str
    direction: str  # in|out

class LinkIssueIn(BaseModel):
    user_id: str

class LinkVerifyIn(BaseModel):
    tg_id: str
    code: str
    device: Optional[str] = "Telegram"
    username: Optional[str] = None
