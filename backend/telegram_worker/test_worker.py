"""Unit tests for the bot's /me formatter (pure, no network)."""
import os
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:token")  # allow import without a real token
from worker import format_me, NOT_LINKED_MSG


def test_format_me_linked_all_fields():
    data = {"linked": True,
            "user": {"name": "Owner", "role": "owner", "branches": None},
            "tg_id": "8970797700", "username": "hassan",
            "linked_at": "2026-07-17T18:24:16+00:00", "status": "connected"}
    t = format_me(data)
    assert "Owner" in t and "owner" in t          # name + role
    assert "All branches" in t                     # null branches -> all
    assert "@hassan" in t                          # telegram username
    assert "8970797700" in t                       # telegram id
    assert "Connected" in t                        # status
    assert "2026" in t                             # linked date rendered


def test_format_me_branches_list_and_username_fallback():
    data = {"linked": True,
            "user": {"name": "BM", "role": "branch_manager", "branches": ["Store A", "Store B"]},
            "tg_id": "1", "username": None}
    t = format_me(data, tg_username="fallback_user")
    assert "Store A, Store B" in t                 # assigned branches list
    assert "@fallback_user" in t                   # falls back to live tg username


def test_format_me_unlinked_is_friendly():
    for d in ({"linked": False}, None, {}):
        t = format_me(d)
        assert t == NOT_LINKED_MSG
        assert "/link" in t                         # tells the user how to link
