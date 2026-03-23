"""
Built-in deterministic row generators for fixed-content TPC benchmark tables.

These generators replace the column-by-column random synthesis path for tables
whose content is fully determined by the benchmark specification — no random
sampling is required.  All implementations use stdlib only.

Exposed entry-point
-------------------
``dispatch(generator_name, row_count, row_offset)``
    Dispatcher used by ``row_generator.generate_rows``.  Returns an
    ``Iterator[dict]`` that yields exactly *row_count* rows starting at
    position *row_offset* (for append-mode support).

Supported generators
--------------------
date_dim              TPC-DS §2.4 — 73,049 calendar rows (1900-01-02 → 2100-01-01).
time_dim              TPC-DS §2.6 — 86,400 second-of-day rows.
customer_demographics TPC-DS §2.7 — 1,920,800-row Cartesian product of 8 attributes.
household_demographics TPC-DS §2.8 — 7,200-row product of 4 attributes.
income_band           TPC-DS §2.9 — 20 fixed income-band rows.
zip_code              TPC-E  §2.2 — 14,741 synthetic US ZIP-code reference rows.
"""

from __future__ import annotations

import itertools
from datetime import date, timedelta
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Any] = {}


def dispatch(
    generator_name: str,
    row_count: int,
    row_offset: int = 0,
) -> Iterator[dict[str, Any]]:
    """
    Yield *row_count* rows from the named built-in generator, skipping the
    first *row_offset* rows (for append loads).
    """
    fn = _REGISTRY.get(generator_name)
    if fn is None:
        raise ValueError(
            f"Unknown builtin_generator '{generator_name}'. "
            f"Valid values: {sorted(_REGISTRY)}"
        )
    gen = fn()
    # skip already-loaded rows (append mode)
    for _ in range(row_offset):
        try:
            next(gen)
        except StopIteration:
            return
    yielded = 0
    for row in gen:
        if yielded >= row_count:
            break
        yield row
        yielded += 1


def _register(name: str):
    def decorator(fn):
        _REGISTRY[name] = fn
        return fn
    return decorator


# ---------------------------------------------------------------------------
# TPC-DS  date_dim  (73,049 rows)
# ---------------------------------------------------------------------------

# US federal holidays (month, day) for the holiday flag — approximation.
_US_FIXED_HOLIDAYS = {(1, 1), (7, 4), (11, 11), (12, 25)}

# Months that start each quarter
_QUARTER_FIRST_MONTH = {1: 1, 2: 1, 3: 1, 4: 4, 5: 4, 6: 4, 7: 7, 8: 7, 9: 7, 10: 10, 11: 10, 12: 10}
_DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
_SHIFT_NAMES = ["First", "Second", "Third", "Fourth"]  # quarter-of-day labels

_DATE_DIM_START = date(1900, 1, 2)
_DATE_DIM_END   = date(2100, 1, 1)
_DATE_DIM_ROWS  = (_DATE_DIM_END - _DATE_DIM_START).days + 1  # 73,049


def _iso_date_id(d: date) -> str:
    """TPC-DS date_id format: AAAAAA<YYYY><MM><DD> — 10-char string."""
    return f"AAAAAA{d.year:04d}{d.month:02d}{d.day:02d}"


def _month_seq(d: date) -> int:
    """Months since year 1900-01: 1900-01 = 0."""
    return (d.year - 1900) * 12 + (d.month - 1)


def _week_seq(d: date) -> int:
    """ISO week number offset from 1900-01-01."""
    ref = date(1900, 1, 1)
    return (d - ref).days // 7


def _quarter_seq(d: date) -> int:
    """Quarters since 1900-Q1 = 0."""
    return (d.year - 1900) * 4 + (d.month - 1) // 3


def _fy_week_seq(d: date) -> int:
    """Fiscal-year week (identical to calendar week for TPC-DS)."""
    return _week_seq(d)


def _is_holiday(d: date) -> bool:
    return (d.month, d.day) in _US_FIXED_HOLIDAYS


def _first_day_of_month_sk(d: date, base: date) -> int:
    """SK of the first day of d's month."""
    return (date(d.year, d.month, 1) - base).days + 1


def _last_day_of_month_sk(d: date, base: date) -> int:
    """SK of the last day of d's month."""
    import calendar as _cal
    last = _cal.monthrange(d.year, d.month)[1]
    return (date(d.year, d.month, last) - base).days + 1


def _same_day_ly_sk(d: date, base: date) -> int:
    """SK of the same calendar date one year prior."""
    try:
        prev = d.replace(year=d.year - 1)
    except ValueError:
        prev = d.replace(year=d.year - 1, day=28)  # Feb-29 → Feb-28
    if prev < base:
        return 1
    return (prev - base).days + 1


def _same_day_lq_sk(d: date, base: date) -> int:
    """SK of the same date three months prior (approximate quarterly rollback)."""
    m = d.month - 3
    y = d.year
    if m < 1:
        m += 12
        y -= 1
    import calendar as _cal
    max_day = _cal.monthrange(y, m)[1]
    prev = date(y, m, min(d.day, max_day))
    if prev < base:
        return 1
    return (prev - base).days + 1


@_register("date_dim")
def _gen_date_dim() -> Iterator[dict[str, Any]]:
    base = _DATE_DIM_START
    today = date.today()
    sk = 1
    cur = base
    while cur <= _DATE_DIM_END:
        dow   = cur.weekday()          # 0=Monday … 6=Sunday in Python
        dow7  = (dow + 1) % 7          # TPC-DS: 0=Sunday … 6=Saturday
        mseq  = _month_seq(cur)
        qseq  = _quarter_seq(cur)
        wseq  = _week_seq(cur)
        qoy   = (cur.month - 1) // 3 + 1
        is_wk = cur.weekday() >= 5     # Saturday or Sunday
        is_hol = _is_holiday(cur)

        yield {
            "d_date_sk":           sk,
            "d_date_id":           _iso_date_id(cur),
            "d_date":              cur.isoformat(),
            "d_month_seq":         mseq,
            "d_week_seq":          wseq,
            "d_quarter_seq":       qseq,
            "d_year":              cur.year,
            "d_dow":               dow7,
            "d_moy":               cur.month,
            "d_dom":               cur.day,
            "d_qoy":               qoy,
            "d_fy_year":           cur.year,
            "d_fy_quarter_seq":    qseq,
            "d_fy_week_seq":       _fy_week_seq(cur),
            "d_day_name":          _DAY_NAMES[dow7],
            "d_quarter_name":      f"{cur.year}Q{qoy}",
            "d_holiday":           "Y" if is_hol else "N",
            "d_weekend":           "Y" if is_wk else "N",
            "d_following_holiday": "Y" if _is_holiday(cur + timedelta(1)) else "N",
            "d_first_dom":         _first_day_of_month_sk(cur, base),
            "d_last_dom":          _last_day_of_month_sk(cur, base),
            "d_same_day_ly":       _same_day_ly_sk(cur, base),
            "d_same_day_lq":       _same_day_lq_sk(cur, base),
            "d_current_day":       "Y" if cur == today else "N",
            "d_current_week":      "Y" if wseq == _week_seq(today) else "N",
            "d_current_month":     "Y" if mseq == _month_seq(today) else "N",
            "d_current_quarter":   "Y" if qseq == _quarter_seq(today) else "N",
            "d_current_year":      "Y" if cur.year == today.year else "N",
        }
        cur += timedelta(1)
        sk  += 1


# ---------------------------------------------------------------------------
# TPC-DS  time_dim  (86,400 rows)
# ---------------------------------------------------------------------------

_SHIFT_BY_HOUR = {
    **{h: "Night"   for h in range(0, 8)},
    **{h: "Day"     for h in range(8, 18)},
    **{h: "Evening" for h in range(18, 24)},
}
_SUB_SHIFT_BY_HOUR = {
    **{h: "Late Night" for h in range(0, 4)},
    **{h: "Night"      for h in range(4, 8)},
    **{h: "Morning"    for h in range(8, 12)},
    **{h: "Afternoon"  for h in range(12, 17)},
    **{h: "Evening"    for h in range(17, 20)},
    **{h: "Late Evening" for h in range(20, 24)},
}
_MEAL_BY_HOUR = {
    7: "Breakfast", 8: "Breakfast",
    12: "Lunch", 13: "Lunch",
    18: "Dinner", 19: "Dinner",
}


def _time_id(h: int, m: int, s: int) -> str:
    return f"AAAAAA{h:02d}{m:02d}{s:02d}"


@_register("time_dim")
def _gen_time_dim() -> Iterator[dict[str, Any]]:
    for sk in range(86400):
        h, rem = divmod(sk, 3600)
        m, s   = divmod(rem, 60)
        yield {
            "t_time_sk":   sk,
            "t_time_id":   _time_id(h, m, s),
            "t_time":      sk,
            "t_hour":      h,
            "t_minute":    m,
            "t_second":    s,
            "t_am_pm":     "AM" if h < 12 else "PM",
            "t_shift":     _SHIFT_BY_HOUR[h],
            "t_sub_shift": _SUB_SHIFT_BY_HOUR[h],
            "t_meal_time": _MEAL_BY_HOUR.get(h),
        }


# ---------------------------------------------------------------------------
# TPC-DS  customer_demographics  (1,920,800 rows)
# Cartesian product: 2×5×7×20×4×7×7×7 = 1,920,800
# ---------------------------------------------------------------------------

_CD_GENDER           = ["M", "F"]
_CD_MARITAL_STATUS   = ["S", "M", "D", "W", "U"]
_CD_EDUCATION_STATUS = [
    "Primary", "Secondary", "College",
    "2 yr Degree", "4 yr Degree", "Advanced Degree", "Unknown",
]
_CD_PURCHASE_ESTIMATE = list(range(500, 10001, 500))   # 500, 1000, …, 10000  (20 values)
_CD_CREDIT_RATING    = ["Good", "High Risk", "Low Risk", "Unknown"]
_CD_DEP_COUNT        = list(range(7))    # 0–6
_CD_DEP_EMP_COUNT    = list(range(7))
_CD_DEP_COLLEGE_COUNT = list(range(7))


@_register("customer_demographics")
def _gen_customer_demographics() -> Iterator[dict[str, Any]]:
    sk = 1
    for g, ms, ed, pe, cr, dc, de, dcc in itertools.product(
        _CD_GENDER, _CD_MARITAL_STATUS, _CD_EDUCATION_STATUS,
        _CD_PURCHASE_ESTIMATE, _CD_CREDIT_RATING,
        _CD_DEP_COUNT, _CD_DEP_EMP_COUNT, _CD_DEP_COLLEGE_COUNT,
    ):
        yield {
            "cd_demo_sk":              sk,
            "cd_gender":               g,
            "cd_marital_status":       ms,
            "cd_education_status":     ed,
            "cd_purchase_estimate":    pe,
            "cd_credit_rating":        cr,
            "cd_dep_count":            dc,
            "cd_dep_employed_count":   de,
            "cd_dep_college_count":    dcc,
        }
        sk += 1


# ---------------------------------------------------------------------------
# TPC-DS  household_demographics  (7,200 rows)
# Cartesian product: 20 income_bands × 6 buy_potential × 7 dep_count × ? = 7,200
# 7200 = 20 × 6 × 6 × 10 or 20 × 6 × 5 × 12 … let's use 20×6×6×10
# Spec: hd_buy_potential (6), hd_dep_count (0–9 = 10), hd_vehicle_count (0–5 = 6)
# 20 × 6 × 10 × 6 = 7,200  ✓
# ---------------------------------------------------------------------------

_HD_BUY_POTENTIAL = [
    "Unknown", "1001-5000", "501-1000", "0-500", ">10000", "5001-10000",
]
_HD_DEP_COUNT     = list(range(10))   # 0–9
_HD_VEH_COUNT     = list(range(6))    # 0–5


@_register("household_demographics")
def _gen_household_demographics() -> Iterator[dict[str, Any]]:
    sk = 1
    for ib in range(1, 21):                    # income_band_sk 1..20
        for bp, dc, vc in itertools.product(
            _HD_BUY_POTENTIAL, _HD_DEP_COUNT, _HD_VEH_COUNT
        ):
            yield {
                "hd_demo_sk":         sk,
                "hd_income_band_sk":  ib,
                "hd_buy_potential":   bp,
                "hd_dep_count":       dc,
                "hd_vehicle_count":   vc,
            }
            sk += 1


# ---------------------------------------------------------------------------
# TPC-DS  income_band  (20 rows)
# ---------------------------------------------------------------------------

@_register("income_band")
def _gen_income_band() -> Iterator[dict[str, Any]]:
    step = 10_000
    for sk in range(1, 21):
        lo = (sk - 1) * step
        hi = lo + step - 1
        yield {
            "ib_income_band_sk": sk,
            "ib_lower_bound":    lo,
            "ib_upper_bound":    hi,
        }


# ---------------------------------------------------------------------------
# TPC-E  zip_code  (14,741 rows)
# Synthetic US ZIP codes: 00001–14741, with plausible city/state assignments.
# ---------------------------------------------------------------------------

_ZC_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
]

_ZC_CITY_PREFIXES = [
    "North", "South", "East", "West", "New", "Old", "Lake", "River",
    "Mount", "Fort", "Port", "Spring", "Fall", "Cedar", "Pine", "Oak",
    "Maple", "Green", "White", "Black",
]
_ZC_CITY_ROOTS = [
    "field", "ville", "town", "burg", "wood", "land", "brook", "creek",
    "valley", "ridge", "hill", "dale", "ford", "port", "grove", "gate",
    "stone", "park", "view", "haven",
]


def _zip_city(n: int) -> str:
    prefix = _ZC_CITY_PREFIXES[n % len(_ZC_CITY_PREFIXES)]
    root   = _ZC_CITY_ROOTS[(n // len(_ZC_CITY_PREFIXES)) % len(_ZC_CITY_ROOTS)]
    return f"{prefix}{root}"


@_register("zip_code")
def _gen_zip_code() -> Iterator[dict[str, Any]]:
    total = 14741
    zips_per_state = total // len(_ZC_STATES)
    for n in range(total):
        state_idx = min(n // max(1, zips_per_state), len(_ZC_STATES) - 1)
        yield {
            "zc_code":        f"{n + 1:05d}",
            "zc_town":        _zip_city(n),
            "zc_div":         _ZC_STATES[state_idx],
            "zc_country":     "US",
            "zc_is_metro":    1 if (n % 5 == 0) else 0,
        }
