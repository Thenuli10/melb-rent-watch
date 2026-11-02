import pandas as pd
import pytest

from src.transform import clean_suburbs as cs


@pytest.fixture
def sal():
    return pd.DataFrame({
        "sal_code": ["1", "2", "3", "4", "5", "6"],
        "sal_name": ["Carlton (Vic.)", "Parkville (Vic.)", "Hawthorn East", "Mount Waverley", "Melbourne",
                     "Kew (Vic.)"],
        "in_gmel": [True, True, True, True, True, True],
    })


@pytest.fixture(autouse=True)
def tmp_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "OVERRIDES", tmp_path / "suburb_overrides.csv")
    monkeypatch.setattr(cs, "CROSSWALK", tmp_path / "suburb_crosswalk.csv")
    monkeypatch.setattr(cs, "UNMATCHED", tmp_path / "unmatched_suburbs.csv")
    return tmp_path


@pytest.mark.parametrize("raw, expected", [
    ("Carlton (Vic.)", "CARLTON"),
    ("  mt  Waverley ", "MOUNT WAVERLEY"),
    ("Carlton–Parkville", "CARLTON-PARKVILLE"),
    ("St Kilda", "ST KILDA"),
])
def test_normalise(raw, expected):
    assert cs.normalise(raw) == expected


def test_split_group_handles_en_dash():
    assert cs.split_group("Carlton–Parkville") == ["Carlton", "Parkville"]


def test_exact_fuzzy_and_abbreviation_matches(sal):
    cw = cs.build_crosswalk(["Carlton-Parkville", "East Hawthorn", "Mt Waverley"], sal)
    got = dict(zip(cw["part"], cw["sal_name"]))
    assert got == {"Carlton": "Carlton (Vic.)", "Parkville": "Parkville (Vic.)",
                   "East Hawthorn": "Hawthorn East", "Mt Waverley": "Mount Waverley"}
    assert cw.set_index("part").loc["East Hawthorn", "match_method"] == "fuzzy"


def test_unmatched_name_fails_loudly(sal, tmp_reference):
    with pytest.raises(cs.UnmatchedSuburbError):
        cs.build_crosswalk(["CBD-St Kilda Rd"], sal)
    unmatched = pd.read_csv(tmp_reference / "unmatched_suburbs.csv")
    assert set(unmatched["part"]) == {"CBD", "St Kilda Rd"}


def test_override_replaces_automatic_split(sal, tmp_reference):
    pd.DataFrame({"suburb_group": ["CBD-St Kilda Rd"], "sal_name": ["Melbourne"], "note": [""]}).to_csv(
        tmp_reference / "suburb_overrides.csv", index=False)
    cw = cs.build_crosswalk(["CBD-St Kilda Rd"], sal)
    assert cw["sal_code"].tolist() == ["5"]
    assert cw["match_method"].tolist() == ["override"]


def test_prefers_greater_melbourne_suburb():
    sal = pd.DataFrame({"sal_code": ["1", "2"], "sal_name": ["Kew (Vic.)", "Kew East"], "in_gmel": [False, True]})
    sal.loc[2] = ["3", "Kew", True]
    cw = cs.build_crosswalk(["Kew"], sal)
    assert cw["sal_code"].tolist() == ["3"]
