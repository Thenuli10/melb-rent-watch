"""Match DFFH suburb groups to ABS Suburbs and Localities (SAL).

1. Normalise names (case, dashes, state suffixes, MT -> MOUNT).
2. Split groups like "Carlton-Parkville" into parts.
3. Match each part: manual override > exact > fuzzy (rapidfuzz token_sort_ratio).
4. Anything unmatched is written to data/reference/unmatched_suburbs.csv and
   the run fails, so new names never silently drop rows.

Output: a crosswalk suburb_group -> sal_code(s), the bridge table in the warehouse.
"""
from __future__ import annotations

import re

import pandas as pd
from rapidfuzz import fuzz, process

from src.common import REFERENCE_DIR, load_config, log

OVERRIDES = REFERENCE_DIR / "suburb_overrides.csv"
CROSSWALK = REFERENCE_DIR / "suburb_crosswalk.csv"
UNMATCHED = REFERENCE_DIR / "unmatched_suburbs.csv"

_SUFFIX = re.compile(r"\s*\((VIC\.?|VICTORIA|MELBOURNE|[A-Z .]+ - VIC\.?)\)\s*$")
_DASHES = re.compile(r"[‐-―−]")
_ABBREV = {"MT": "MOUNT", "MT.": "MOUNT", "ST.": "ST", "STH": "SOUTH", "NTH": "NORTH"}


class UnmatchedSuburbError(RuntimeError):
    pass


def normalise(name: str) -> str:
    s = _DASHES.sub("-", str(name)).upper().strip()
    s = _SUFFIX.sub("", s)
    s = re.sub(r"\s+", " ", s)
    return " ".join(_ABBREV.get(tok, tok) for tok in s.split(" "))


def split_group(group: str, sep: str = "-") -> list[str]:
    return [p.strip() for p in _DASHES.sub("-", group).split(sep) if p.strip()]


def load_overrides() -> pd.DataFrame:
    if not OVERRIDES.exists():
        return pd.DataFrame(columns=["suburb_group", "sal_name", "note"])
    return pd.read_csv(OVERRIDES, dtype=str).fillna("")


def build_crosswalk(groups: list[str], sal: pd.DataFrame) -> pd.DataFrame:
    """Map each suburb group to one or more SALs.

    `sal` needs columns sal_code, sal_name, in_gmel. Matching prefers SALs
    inside Greater Melbourne, so a Melbourne suburb never matches a
    same-named country town.
    """
    cfg = load_config()["matching"]
    sal = sal.copy()
    sal["norm"] = sal["sal_name"].map(normalise)
    gmel = sal[sal["in_gmel"]]
    by_norm_gmel = gmel.drop_duplicates("norm").set_index("norm")
    by_norm_vic = sal.drop_duplicates("norm").set_index("norm")
    by_name = sal.set_index("sal_name")

    overrides = load_overrides()
    rows, unmatched = [], []

    for group in sorted(set(groups)):
        ov = overrides[overrides["suburb_group"] == group]
        if not ov.empty:
            for name in ov["sal_name"]:
                if name not in by_name.index:
                    unmatched.append({"suburb_group": group, "part": name, "reason": "override sal_name not in SAL list",
                                      "best_candidate": "", "score": None})
                    continue
                hit = by_name.loc[name]
                rows.append({"suburb_group": group, "part": name, "sal_code": hit["sal_code"], "sal_name": name,
                             "match_method": "override", "match_score": 100.0})
            continue

        for part in split_group(group, cfg["group_separator"]):
            key = normalise(part)
            if key in by_norm_gmel.index:
                hit, method, score = by_norm_gmel.loc[key], "exact", 100.0
            elif key in by_norm_vic.index:
                hit, method, score = by_norm_vic.loc[key], "exact_outside_gmel", 100.0
            else:
                best = process.extractOne(key, gmel["norm"].tolist(), scorer=fuzz.token_sort_ratio)
                if best and best[1] >= cfg["fuzzy_threshold"]:
                    hit, method, score = by_norm_gmel.loc[best[0]], "fuzzy", float(best[1])
                else:
                    unmatched.append({"suburb_group": group, "part": part, "reason": "no match above threshold",
                                      "best_candidate": best[0] if best else "", "score": best[1] if best else None})
                    continue
            rows.append({"suburb_group": group, "part": part, "sal_code": hit["sal_code"], "sal_name": hit["sal_name"],
                         "match_method": method, "match_score": score})

    if unmatched:
        pd.DataFrame(unmatched).to_csv(UNMATCHED, index=False)
        raise UnmatchedSuburbError(
            f"{len(unmatched)} suburb name(s) could not be matched; see {UNMATCHED.name} and add rows to "
            f"{OVERRIDES.name}")
    UNMATCHED.unlink(missing_ok=True)

    cw = pd.DataFrame(rows).drop_duplicates(["suburb_group", "sal_code"])
    fuzzy = cw[cw["match_method"] != "exact"]
    if not fuzzy.empty:
        log.info("Non-exact suburb matches to review:\n%s", fuzzy.to_string(index=False))
    cw.to_csv(CROSSWALK, index=False)
    return cw
