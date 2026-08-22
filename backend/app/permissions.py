"""Role → permission map + branch scope. Mirrors the SmokeStack web app exactly,
so the backend enforces the same rules the UI shows."""

ALL_PERMS = [
    "view","create","edit","delete","approve","void","refund","print","export",
    "view_cost","edit_cost","view_profit","view_payroll","manage_users","manage_permissions","manage_telegram_links",
    "manage_branches","manage_telegram_links","transfer_stock","adjust_stock","close_shift","view_all_branches",
    "scan_barcode","print_labels","continuous_receiving","add_employee","edit_employee",
    "deactivate_employee","run_payroll","finalize_payroll","view_inventory_history","view_asof","export_history",
    "chat_view","chat_send","chat_create_room","chat_manage_room","chat_delete_message",
    "chat_pin","chat_create_task","chat_announce","chat_company_room",
    "manage_reminders","manage_schedules",
]

PERMS = {
    "owner": list(ALL_PERMS),
    "admin": ["view","create","edit","delete","approve","void","refund","print","export","view_cost","view_profit","view_payroll","manage_branches","manage_telegram_links","transfer_stock","adjust_stock","close_shift","view_all_branches","scan_barcode","print_labels","continuous_receiving","add_employee","edit_employee","deactivate_employee","run_payroll","finalize_payroll","view_inventory_history","view_asof","export_history","manage_reminders","manage_schedules"],
    "branch_manager": ["view","create","edit","approve","void","print","export","view_cost","transfer_stock","adjust_stock","close_shift","scan_barcode","print_labels","continuous_receiving","add_employee","edit_employee","run_payroll","view_inventory_history","view_asof","export_history","manage_schedules",
        "chat_view","chat_send","chat_create_room","chat_manage_room","chat_pin","chat_create_task","chat_announce","chat_company_room"],
    "manager": ["view","create","edit","approve","void","print","export","view_cost","transfer_stock","adjust_stock","close_shift","scan_barcode","print_labels","continuous_receiving","add_employee","edit_employee","run_payroll","view_inventory_history","view_asof","export_history","manage_schedules"],
    "cashier": ["view","create","refund","print","close_shift","scan_barcode","chat_view","chat_send","chat_create_task"],
    "inventory_manager": ["view","create","edit","print","export","transfer_stock","adjust_stock","view_cost","scan_barcode","print_labels","continuous_receiving","view_inventory_history","view_asof","export_history","chat_view","chat_send","chat_create_room","chat_create_task"],
    "accountant": ["view","create","edit","void","refund","print","export","view_cost","view_profit","view_payroll","view_all_branches","add_employee","edit_employee","deactivate_employee","run_payroll","finalize_payroll","view_inventory_history","view_asof","export_history","chat_view","chat_send","chat_create_room","chat_create_task","chat_company_room"],
    "employee": ["view","print","chat_view","chat_send","chat_create_task"],
}
ALLBRANCH_ROLES = ["owner", "admin", "accountant"]

def can(role: str, perm: str) -> bool:
    return perm in PERMS.get(role, [])

def allowed_branches(user, all_branches):
    """The branches this user may touch: their explicit assignments, or every
    branch for an all-branch role.

    BF-14: a branch-scoped user with NO assignment previously fell through to
    ``all_branches[:1]`` — silently granting whichever branch happened to sort
    first, with read AND write access. Forgetting to assign a branch has to fail
    closed, or branch assignment is not a control at all. An unassigned
    branch-scoped user now resolves to the EMPTY set: reads return nothing and
    every write is refused by ``assert_branch``.
    """
    ub = getattr(user, "branch_names", None)
    if ub:
        return list(ub)
    if user.role in ALLBRANCH_ROLES:
        return list(all_branches)
    return []

def can_see_all(role: str) -> bool:
    return can(role, "view_all_branches") or role in ALLBRANCH_ROLES
