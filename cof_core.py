"""Core logic for the Cost-of-Funds calculator: PDF extraction, database storage, calculation."""
from __future__ import annotations

import bisect
import datetime as dt
import re
import sqlite3
from dataclasses import dataclass

import pandas as pd

# ----------------------------------------------------------------------------------------
# 1. PDF extraction  (Name, Date, Balance)
# ----------------------------------------------------------------------------------------
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"

_DATE_PATTERNS = [
    ("ymd", re.compile(r"(?<![\d/.\-])(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")),
    ("dmon", re.compile(rf"(?<![\w/.\-])(\d{{1,2}})[-/.\s]*({_MON})[a-z]*[-/.\s,]*(\d{{2,4}})(?!\d)", re.I)),
    ("num", re.compile(r"(?<![\d/.\-])(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})(?!\d)")),
]


def _year(y: str) -> int:
    y = int(y)
    return y + 2000 if y < 100 else y


def find_dates(line: str, dayfirst: bool = True):
    """Return list of (start, end, date) for every date found in the line, left to right."""
    found = []
    for kind, rx in _DATE_PATTERNS:
        for m in rx.finditer(line):
            try:
                if kind == "ymd":
                    d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                elif kind == "dmon":
                    d = dt.date(_year(m.group(3)), MONTHS[m.group(2).lower()[:3]], int(m.group(1)))
                else:
                    a, b, y = int(m.group(1)), int(m.group(2)), _year(m.group(3))
                    d = dt.date(y, b, a) if dayfirst else dt.date(y, a, b)
            except ValueError:
                continue
            found.append((m.start(), m.end(), d))
    # drop overlaps, keep the earliest/longest
    found.sort(key=lambda t: (t[0], -(t[1] - t[0])))
    out, last_end = [], -1
    for s, e, d in found:
        if s >= last_end:
            out.append((s, e, d))
            last_end = e
    return out


def _amount_regex(require_decimals: bool):
    frac = r"\.\d{2}" if require_decimals else r"(?:\.\d{1,2})?"
    return re.compile(
        rf"(?<![\w.,])(\(?-?(?:\d{{1,3}}(?:,\d{{3}})+|\d+){frac}\)?)(?:\s*(Dr|Cr)\b\.?)?(?![\w])", re.I)


def parse_amount(token: str, drcr: str | None) -> float:
    neg = token.startswith("(") or token.startswith("-")
    val = float(token.strip("()-").replace(",", ""))
    if neg or (drcr and drcr.lower() == "dr"):
        val = -val
    return val


_NAME_RX = re.compile(
    r"^\s*(?:account\s*(?:title|name|holder)|a/?c\s*(?:title|name)|customer\s*name|borrower\s*name|"
    r"party\s*name|title\s*of\s*account|name\s*of\s*(?:the\s*)?(?:customer|account\s*holder|borrower|account)|name)"
    r"\s*[:\-]\s*(.+)$", re.I)
_NAME_CUT = re.compile(
    r"\s{2,}|\s(?:A/?C\b|Account\s*(?:No|Number|#)|CNIC|Branch|Address|Currency|Cust(?:omer)?\s*(?:ID|No))", re.I)


def extract_name(lines: list[str]) -> str | None:
    for ln in lines[:80]:
        m = _NAME_RX.match(ln)
        if m:
            name = _NAME_CUT.split(m.group(1).strip())[0].strip(" :-")
            if name:
                return name
    return None


@dataclass
class Extraction:
    name: str | None
    rows: pd.DataFrame          # columns: txn_date (date), balance (float)
    pages: int
    warnings: list[str]


def extract_statement(file_obj, dayfirst: bool = True, require_decimals: bool = True) -> Extraction:
    """Read a text-based PDF statement and return Name + (Date, Balance) rows."""
    import pdfplumber

    lines: list[str] = []
    with pdfplumber.open(file_obj) as pdf:
        pages = len(pdf.pages)
        for p in pdf.pages:
            txt = p.extract_text() or ""
            lines.extend(txt.splitlines())
    warnings: list[str] = []
    if not any(l.strip() for l in lines):
        warnings.append("No text found in the PDF - it may be a scanned image (OCR is not included).")
    amt_rx = _amount_regex(require_decimals)
    rows = []
    for ln in lines:
        dates = find_dates(ln, dayfirst)
        if not dates:
            continue
        stripped = ln
        for s, e, _ in reversed(dates):                 # blank out dates so they are not read as amounts
            stripped = stripped[:s] + " " * (e - s) + stripped[e:]
        amts = list(amt_rx.finditer(stripped))
        if not amts:
            continue
        m = amts[-1]                                     # last amount on the line = running balance
        rows.append((dates[0][2], parse_amount(m.group(1), m.group(2))))
    df = pd.DataFrame(rows, columns=["txn_date", "balance"])
    if len(df) >= 2 and df["txn_date"].iloc[0] > df["txn_date"].iloc[-1]:
        df = df.iloc[::-1].reset_index(drop=True)       # statement printed newest-first -> make oldest-first
        warnings.append("Statement appears to be printed newest-first; rows were reversed to oldest-first.")
    if df.empty and not warnings:
        warnings.append("No lines with a date and an amount were recognised. Try changing the parser options.")
    return Extraction(extract_name(lines), df, pages, warnings)


# ----------------------------------------------------------------------------------------
# 2. Database  (SQLite — cross-platform, works on Streamlit Cloud)
# ----------------------------------------------------------------------------------------
TABLE = "statement_balances"


class Store:
    """Stores (account_name, txn_date, balance) rows using SQLite."""

    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._init_table()

    # -- connection ---------------------------------------------------------------------
    def _init_table(self):
        self.conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT,
            txn_date TEXT,
            balance_amt REAL,
            seq INTEGER,
            source_file TEXT,
            uploaded_at TEXT)""")
        self.conn.commit()

    # -- operations ---------------------------------------------------------------------
    def save(self, account_name: str, source_file: str, rows: pd.DataFrame) -> int:
        """Replace any earlier upload of the same file for this account, then insert rows."""
        cur = self.conn.cursor()
        cur.execute(f"DELETE FROM {TABLE} WHERE account_name = ? AND source_file = ?", (account_name, source_file))
        now = dt.datetime.now().isoformat()
        data = []
        for i, (d, b) in enumerate(zip(rows["txn_date"], rows["balance"])):
            d = pd.Timestamp(d).to_pydatetime()
            data.append((account_name, d.isoformat(), float(b), i, source_file, now))
        cur.executemany(
            f"INSERT INTO {TABLE} (account_name, txn_date, balance_amt, seq, source_file, uploaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", data)
        self.conn.commit()
        return len(data)

    def accounts(self) -> list[str]:
        cur = self.conn.cursor()
        cur.execute(f"SELECT DISTINCT account_name FROM {TABLE} ORDER BY account_name")
        return [r[0] for r in cur.fetchall()]

    def load(self, account_name: str) -> pd.DataFrame:
        cur = self.conn.cursor()
        cur.execute(f"SELECT txn_date, balance_amt, seq, source_file FROM {TABLE} WHERE account_name = ?",
                    (account_name,))
        df = pd.DataFrame([tuple(r) for r in cur.fetchall()], columns=["txn_date", "balance", "seq", "source_file"])
        if not df.empty:
            df["txn_date"] = pd.to_datetime(df["txn_date"]).dt.date
        return df

    def delete_account(self, account_name: str):
        cur = self.conn.cursor()
        cur.execute(f"DELETE FROM {TABLE} WHERE account_name = ?", (account_name,))
        self.conn.commit()


# ----------------------------------------------------------------------------------------
# 3. Cost-of-funds calculation
# ----------------------------------------------------------------------------------------
def year_label(d: dt.date, fiscal: bool) -> str:
    if not fiscal:
        return str(d.year)
    y = d.year if d.month >= 7 else d.year - 1
    return f"FY{y}-{str(y + 1)[-2:]}"


def _boundaries(start: dt.date, end_excl: dt.date, fiscal: bool) -> list[dt.date]:
    months = (7,) if fiscal else (1,)
    out = []
    for y in range(start.year - 1, end_excl.year + 2):
        for m in months:
            d = dt.date(y, m, 1)
            if start < d < end_excl:
                out.append(d)
    return out


def period_end_exclusive(end_date: dt.date, include_end: bool) -> dt.date:
    return end_date + dt.timedelta(days=1) if include_end else end_date


def labels_for_period(start: dt.date, end_date: dt.date, fiscal: bool, include_end: bool) -> list[str]:
    end_excl = period_end_exclusive(end_date, include_end)
    pts = [start] + _boundaries(start, end_excl, fiscal)
    seen, out = set(), []
    for p in pts:
        lab = year_label(p, fiscal)
        if lab not in seen:
            seen.add(lab)
            out.append(lab)
    return out


def _day_basis(d: dt.date, convention: str) -> int:
    if convention == "Actual/360":
        return 360
    if convention == "Actual/Actual":
        return 366 if (d.year % 4 == 0 and (d.year % 100 != 0 or d.year % 400 == 0)) else 365
    return 365


def daily_closing(df: pd.DataFrame, use_abs: bool) -> pd.DataFrame:
    """One closing balance per date (last entry of the day, in statement order)."""
    d = df.sort_values(["txn_date", "source_file", "seq"]).groupby("txn_date", as_index=False).last()
    d["balance"] = d["balance"].abs() if use_abs else d["balance"]
    return d[["txn_date", "balance"]]


def compute_cost_of_funds(df, default_date, end_date, rates, convention="Actual/365", fiscal=False,
                          include_end=False, use_abs=True):
    """Return (schedule, summary, warnings).  rates: {year_label: annual % rate}."""
    warnings: list[str] = []
    if end_date <= default_date and not include_end:
        raise ValueError("Ending date must be after the date of default.")
    if end_date < default_date:
        raise ValueError("Ending date cannot be before the date of default.")
    if df.empty:
        raise ValueError("No statement data for this account.")
    closing = daily_closing(df, use_abs)
    dates = list(closing["txn_date"])
    bals = list(closing["balance"])
    if default_date < dates[0]:
        warnings.append(f"Date of default is before the first statement date ({dates[0]:%d-%m-%Y}); "
                        "balance is treated as 0 until then.")
    if period_end_exclusive(end_date, include_end) - dt.timedelta(days=1) > dates[-1]:
        warnings.append(f"Statement data ends on {dates[-1]:%d-%m-%Y}; the last balance is carried forward "
                        f"to the ending date.")

    end_excl = period_end_exclusive(end_date, include_end)
    pts = {default_date, end_excl}
    pts.update(d for d in dates if default_date < d < end_excl)
    pts.update(_boundaries(default_date, end_excl, fiscal))
    pts = sorted(pts)

    rows = []
    for a, b in zip(pts[:-1], pts[1:]):
        i = bisect.bisect_right(dates, a) - 1
        bal = bals[i] if i >= 0 else 0.0
        lab = year_label(a, fiscal)
        rate = rates.get(lab)
        if rate is None:
            raise ValueError(f"Missing cost-of-funds rate for {lab}.")
        days = (b - a).days
        cof = bal * (rate / 100.0) * days / _day_basis(a, convention)
        rows.append({"From": a, "To": b - dt.timedelta(days=1), "Year": lab, "Days": days,
                     "Balance": bal, "Rate %": rate, "Cost of Funds": cof})
    sched = pd.DataFrame(rows)
    sched["Balance x Days"] = sched["Balance"] * sched["Days"]
    g = sched.groupby("Year", sort=False)
    summ = pd.DataFrame({
        "Days": g["Days"].sum(),
        "Average Balance": g["Balance x Days"].sum() / g["Days"].sum(),
        "Rate %": g["Rate %"].first(),
        "Cost of Funds": g["Cost of Funds"].sum(),
    }).reset_index()
    sched = sched.drop(columns="Balance x Days")
    return sched, summ, warnings