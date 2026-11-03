"""Checks for the generated Power BI project. Run after build_powerbi, before opening it:

    python -m src.report.check_powerbi

1. Schema: every report JSON file validates against Microsoft's published PBIR schemas.
2. Blank traps: DAX treats BLANK as 0 in comparisons, so `IF ( [Rank] <= 15, 1 )` is true
   for rows with no rank. In a visual, any row with a non-blank measure is shown, so that
   bug resurrects rows that filters should have removed. Measures that compare a value with
   a number in a blank-sensitive way must test ISBLANK.
"""
from __future__ import annotations

import json
import re
import sys
from functools import lru_cache

from src.common import ROOT

PBIR_DIR = ROOT / "powerbi"
RAW = "https://raw.githubusercontent.com/microsoft/json-schemas/main/"

# `[Measure] <= 15`, `k < 3`, `x = 0` ... : left side, operator, number
_COMPARISON = re.compile(r"(\[[^\]]+\]|\b[a-z]\w*)\s*(<=|<|>=|=)\s*(-?\d+(?:\.\d+)?)\b")


def blank_traps(measures: dict[str, list[tuple]]) -> list[str]:
    """Measures with a comparison that BLANK would pass, and no ISBLANK guard."""
    problems = []
    for table, items in measures.items():
        for name, expr, _fmt in items:
            if "ISBLANK" in expr:
                continue
            for left, op, num in _COMPARISON.findall(expr):
                n = float(num)
                risky = (op in ("<", "<=") and n >= 0) or (op == "=" and n == 0) or (op == ">=" and n <= 0)
                if risky:
                    problems.append(f"{table}[{name}]: '{left} {op} {num}' is true for BLANK; add ISBLANK")
    return problems


@lru_cache(None)
def _fetch(uri: str) -> dict:
    import requests

    return requests.get(uri.replace("https://developer.microsoft.com/json-schemas/", RAW), timeout=30).json()


def schema_errors() -> list[str]:
    from jsonschema import Draft202012Validator, validators
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    reg = Registry(retrieve=lambda u: Resource.from_contents(_fetch(u), default_specification=DRAFT202012))
    files = [f for f in PBIR_DIR.rglob("*") if f.suffix in (".json", ".pbir", ".pbip", ".pbism")
             and ".pbi" not in f.parts and "BaseThemes" not in f.parts]
    errors = []
    for f in sorted(files):
        doc = json.loads(f.read_text(encoding="utf-8"))
        schema = _fetch(doc["$schema"])
        validator = validators.validator_for(schema, default=Draft202012Validator)(schema, registry=reg)
        for e in validator.iter_errors(doc):
            errors.append(f"{f.relative_to(PBIR_DIR)}: {'/'.join(map(str, e.absolute_path))}: {e.message[:160]}")
    return errors


def main() -> int:
    from src.report.build_powerbi import MEASURES

    problems = blank_traps(MEASURES) + schema_errors()
    for p in problems:
        print("FAIL", p)
    print(f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
