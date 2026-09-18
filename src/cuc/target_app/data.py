"""Seeded fake data for the target app. Nothing here is real."""
from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field

# Operator accounts. Fake credentials, documented in README for the demo.
USERS: dict[str, dict[str, str]] = {
    "teller1": {"password": "Passw0rd!", "role": "teller"},
    "auditor1": {"password": "Passw0rd!", "role": "auditor"},  # read-only role
}

PRODUCT_TYPES: list[tuple[str, str]] = [
    ("SAV", "Savings"),
    ("MMK", "Money Market"),
    ("CRT", "Share Certificate"),
]


@dataclass
class Account:
    suffix: str
    product: str  # human label, e.g. "Savings"
    number: str
    balance_cents: int
    nickname: str = ""

    @property
    def balance(self) -> str:
        dollars, cents = divmod(self.balance_cents, 100)
        return f"${dollars:,}.{cents:02d}"


@dataclass
class Member:
    number: str
    first: str
    last: str
    status: str
    ssn_last4: str
    accounts: list[Account] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.last}, {self.first}"

    @property
    def masked_ssn(self) -> str:
        return f"***-**-{self.ssn_last4}"

    def savings(self) -> Account | None:
        return next((a for a in self.accounts if a.product == "Savings"), None)


def _seed() -> dict[str, Member]:
    rows = [
        ("10023", "Avery", "Testerson", "Active", "4821", 125075, 31020),
        ("10047", "Jordan", "Sampleton", "Active", "1190", 980000, 0),
        ("10111", "Riley", "Placeholder", "Dormant", "7734", 1502, 25000),
        ("10500", "Casey", "Mockwell", "Restricted", "0009", 0, 4400),
    ]
    members: dict[str, Member] = {}
    for num, first, last, status, ssn4, sav, chk in rows:
        m = Member(num, first, last, status, ssn4)
        m.accounts.append(Account("S-01", "Savings", f"{num}0001", sav))
        m.accounts.append(Account("S-02", "Checking", f"{num}0002", chk))
        members[num] = m
    return members


MEMBERS: dict[str, Member] = _seed()
_lock = threading.Lock()
_confirmation_seq = itertools.count(700001)


def find_member(number: str) -> Member | None:
    return MEMBERS.get(number.strip())


def open_sub_account(member: Member, product_code: str, nickname: str, deposit_cents: int) -> tuple[Account, str]:
    """Mutates in-memory state. This is the irreversible step of the flow."""
    label = dict(PRODUCT_TYPES)[product_code]
    with _lock:
        suffix = f"S-{len(member.accounts) + 1:02d}"
        acct = Account(suffix, label, f"{member.number}{len(member.accounts) + 1:04d}", deposit_cents, nickname)
        member.accounts.append(acct)
        confirmation = f"CNF-{next(_confirmation_seq)}"
    return acct, confirmation


def reset() -> None:
    """Test helper: restore the seed."""
    global MEMBERS
    MEMBERS.clear()
    MEMBERS.update(_seed())
