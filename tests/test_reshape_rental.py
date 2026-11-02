import pandas as pd
import pytest

from src.common import load_config
from src.transform.reshape_rental import LayoutError, parse_quarter_label, reshape_sheet


def make_sheet(extra_title_rows: int = 0) -> pd.DataFrame:
    """A small sheet laid out like the DFFH workbook (read with header=None)."""
    rows = [["Moving annual median rent by suburb and town", None, "Lease commenced in year ending", None, None, None]]
    rows += [[None] * 6 for _ in range(extra_title_rows)]
    rows += [
        ["1 bedroom flat", None, "Mar 2026", "Mar 2026", "Jun 2026", "Jun 2026"],
        [None, None, "Count", "Median", "Count", "Median"],
        ["Inner Melbourne", "Carlton-Parkville", 300, 420, 310, 430],
        [None, "Docklands", 5, "-", 120, 600],
        [None, "Group Total", 305, 425, 430, 500],
        ["Geelong", "Corio", 50, 300, 52, 310],
        [None, None, None, None, None, None],
    ]
    return pd.DataFrame(rows)


def test_parse_quarter_label():
    assert parse_quarter_label("Mar 2026") == pd.Timestamp("2026-03-31")
    assert parse_quarter_label("Dec 2000") == pd.Timestamp("2000-12-31")
    with pytest.raises(ValueError):
        parse_quarter_label("Feb 2026")


@pytest.mark.parametrize("offset", [0, 2])
def test_reshape_sheet_detects_headers_and_melts(offset):
    out = reshape_sheet(make_sheet(offset), load_config()["rental"])
    assert set(out.columns) == {"region", "suburb_group", "quarter_end", "median_rent", "new_bonds"}
    # 3 suburb groups x 2 quarters; Group Total is dropped
    assert len(out) == 6
    assert "Group Total" not in set(out["suburb_group"])

    carlton = out[out["suburb_group"] == "Carlton-Parkville"].set_index("quarter_end")
    assert carlton.loc[pd.Timestamp("2026-06-30"), "median_rent"] == 430
    assert carlton.loc[pd.Timestamp("2026-06-30"), "new_bonds"] == 310
    # Region forward-filled from the first row of the block
    assert (out[out["suburb_group"] == "Docklands"]["region"] == "Inner Melbourne").all()


def test_suppressed_cells_become_null_not_zero():
    out = reshape_sheet(make_sheet(), load_config()["rental"])
    cell = out[(out["suburb_group"] == "Docklands") & (out["quarter_end"] == pd.Timestamp("2026-03-31"))]
    assert cell["median_rent"].isna().all()


def test_missing_header_raises():
    sheet = make_sheet()
    sheet.iloc[2, 3] = "Average"  # metric row no longer matches
    sheet.iloc[2, 2] = "Average"
    sheet.iloc[2, 4] = "Average"
    sheet.iloc[2, 5] = "Average"
    with pytest.raises(LayoutError):
        reshape_sheet(sheet, load_config()["rental"])
