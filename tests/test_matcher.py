"""Scored matcher: features, training, calibration bands, threshold picking, ladder hook.
All training here uses a generated toy set in tmp_path; the repo's models/ and config.yaml
are never touched."""

from __future__ import annotations

import csv
import itertools
import json
import random
import shutil
from pathlib import Path

import pytest

from app.config import DEFAULT_CONFIG_PATH, Settings, get_settings, load_settings
from app.identity import ladder
from app.identity import train as tr
from app.identity.ladder import NO_ENTITY, SEVERAL, AccountIn, Outcome
from app.identity.matcher import (
    FEATURES,
    Band,
    band,
    director_overlap,
    domain_email_match,
    features,
    load_model,
    score,
)
from app.identity.step import IdentityDeps, identify
from tests.discovery_helpers import FakeSave
from tests.identity_helpers import FakeFetch, FakeRegistry, mca

S = load_settings(DEFAULT_CONFIG_PATH)

FIRST = ["Acme", "Sharma", "Patel", "Bharat", "Ganesh", "Shree", "Kaveri", "Ganga", "Indus",
         "Sagar", "Vardhman", "Tirupati", "Balaji", "Krishna", "Lakshmi", "Navkar", "Mahavir",
         "Saraswati", "Godavari", "Narmada"]  # fmt: skip
SECOND = ["Logistics", "Foods", "Textiles", "Steel", "Pharma", "Retail"]
PLACES = [("Mumbai", "Maharashtra"), ("Pune", "Maharashtra"), ("Chennai", "Tamil Nadu"),
          ("Delhi", "Delhi"), ("Ahmedabad", "Gujarat"), ("Bengaluru", "Karnataka")]  # fmt: skip


def toy_pairs(n: int = 60, seed: int = 1) -> list[tr.Pair]:
    """Positives: the same firm written differently. Negatives: another firm, or the same
    name in another city and state."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        name = f"{FIRST[i % len(FIRST)]} {SECOND[(i // len(FIRST)) % len(SECOND)]}"
        city, state = PLACES[i % len(PLACES)]
        left = {"name": f"{name} Pvt Ltd", "city": city, "state": state}
        variant = rng.choice([name.upper() + " PRIVATE LIMITED", name + " Limited", name[:-1]])
        out.append(tr.Pair(left, {"name": variant, "city": city, "state": state}, 1))
        if i % 2:
            other = f"{FIRST[(i + 7) % len(FIRST)]} {SECOND[(i + 3) % len(SECOND)]} Limited"
            out.append(tr.Pair(left, {"name": other, "city": city, "state": state}, 0))
        else:
            ocity, ostate = PLACES[(i + 3) % len(PLACES)]
            out.append(tr.Pair(left, {"name": name.upper() + " LIMITED", "city": ocity,
                                      "state": ostate}, 0))  # fmt: skip
    return out


def write_csv(path: Path, pairs: list[tr.Pair]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["left", "right", "label"])
        w.writeheader()
        for p in pairs:
            w.writerow({"left": json.dumps(p.left), "right": json.dumps(p.right), "label": p.label})
    return path


def tmp_settings(tmp_path: Path) -> Settings:
    m = S.matching.model_copy(update={"model_path": str(tmp_path / "matcher.joblib"),
                                      "training_csv": str(tmp_path / "pairs.csv")})  # fmt: skip
    return S.model_copy(update={"matching": m})


@pytest.fixture
def trained(tmp_path: Path) -> tuple[Settings, tr.TrainResult, Path]:
    s = tmp_settings(tmp_path)
    write_csv(tmp_path / "pairs.csv", toy_pairs())
    cfg = tmp_path / "config.yaml"
    shutil.copy(DEFAULT_CONFIG_PATH, cfg)
    result = tr.train(s, config_path=cfg)
    return s, result, cfg


# --- features -----------------------------------------------------------------------------


def test_feature_vector_is_complete_and_tri_state() -> None:
    f = features({"name": "Honasa Consumer Ltd", "state": "Delhi", "city": "New Delhi",
                  "domain": "mamaearth.in", "gstin": "07AAFCH1234B1Z5"},
                 {"name": "HONASA CONSUMER LIMITED", "state": "Haryana",
                  "email": "cs@mamaearth.in", "pan": "AAFCH1234B"}, S)  # fmt: skip
    assert tuple(f) == FEATURES
    assert f["exact_name"] == 1.0 and f["jaro_winkler"] == 1.0
    assert f["state_match"] == -1.0  # known and different
    assert f["city_match"] == 0.0  # right city unknown: no evidence
    assert f["domain_email_match"] == 1.0 and f["pan_equal"] == 1.0
    assert f["director_overlap"] == 0.0


def test_helpers() -> None:
    assert domain_email_match("acme.in", "info@mail.acme.in") is True
    assert domain_email_match("acme.in", "acme@gmail.com") is False
    assert domain_email_match(None, "x@acme.in") is None
    assert director_overlap(["Varun Alagh", "Ghazal Alagh"], ["ghazal  alagh"]) == 0.0  # 1/2
    assert director_overlap(["A B"], ["C D"]) == -1.0


# --- training -----------------------------------------------------------------------------


def test_model_loads_and_scores(trained) -> None:
    s, result, _ = trained
    bundle = load_model(s)
    assert bundle is not None and tuple(bundle["features"]) == FEATURES
    p = score(features({"name": "Acme Foods Pvt Ltd"}, {"name": "ACME FOODS LIMITED"}, s), s)
    assert p is not None and 0.0 <= p <= 1.0
    assert result.n_synthetic > 0 and result.n_holdout > 0


def test_no_model_scores_none(tmp_path: Path) -> None:
    s = tmp_settings(tmp_path)
    assert load_model(s) is None
    assert score(features({"name": "a"}, {"name": "a"}, s), s) is None


def test_scores_monotonic_in_name_similarity(trained) -> None:
    s, _, _ = trained
    left = {"name": "Acme Foods Pvt Ltd", "city": "Delhi", "state": "Delhi"}
    rights = ["Zeta Steel Limited", "Acme Steel Limited", "Acme Food", "ACME FOODS LIMITED"]
    ps = [score(features(left, {"name": r, "city": "Delhi", "state": "Delhi"}, s), s)
          for r in rights]  # fmt: skip
    assert all(a <= b + 1e-9 for a, b in itertools.pairwise(ps)), ps
    assert ps[-1] > ps[0]


def test_threshold_written_back_and_respected(trained) -> None:
    s, result, cfg = trained
    text = cfg.read_text()
    line = next(x for x in text.splitlines() if x.strip().startswith("match_threshold:"))
    assert f"match_threshold: {result.match_threshold:.4f}" in line
    assert "set " in line and "held-out pairs" in line and "labelled" in line
    assert load_settings(cfg).matching.match_threshold == pytest.approx(
        round(result.match_threshold, 4)
    )
    assert result.match_threshold > s.matching.nomatch_threshold
    # The match band is 100% precise on the held-out pairs by construction.
    match_band = result.report["match"]
    assert match_band["n"] == 0 or match_band["precision"] == 1.0
    assert abs(sum(b["share"] for b in result.report.values()) - 1.0) < 1e-9


def test_pick_match_threshold() -> None:
    assert tr.pick_match_threshold([0.95, 0.9, 0.8, 0.7], [1, 1, 0, 1]) == 0.9
    assert tr.pick_match_threshold([0.9, 0.9, 0.2], [1, 0, 0]) is None  # tie with a negative
    assert tr.pick_match_threshold([0.99, 0.3], [0, 1]) is None


def test_bands_respect_config() -> None:
    m = S.matching
    assert band(m.match_threshold, S) is Band.match
    assert band(m.nomatch_threshold, S) is Band.no_match
    assert band((m.match_threshold + m.nomatch_threshold) / 2, S) is Band.held


def test_band_report() -> None:
    r = tr.band_report([0.95, 0.92, 0.7, 0.2, 0.1], [1, 1, 1, 0, 1], 0.9, 0.5)
    assert r["match"] == {"n": 2, "share": 0.4, "precision": 1.0, "recall": 0.5}
    assert r["no_match"]["precision"] == 0.5 and r["no_match"]["recall"] == 1.0
    assert r["held"]["share"] == 0.2 and r["held"]["precision"] is None


def test_synthetic_negatives_perturb_right_side() -> None:
    pos = [p for p in toy_pairs(12) if p.label == 1]
    neg = tr.synthetic_negatives(pos, 3, "Logistics", seed=7)
    assert neg and all(n.label == 0 and n.source.startswith("synthetic:") for n in neg)
    kinds = {n.source.split(":")[1] for n in neg}
    assert kinds == {"swap_token", "add_token", "change_city"}
    originals = {json.dumps(p.right, sort_keys=True) for p in pos}
    assert all(json.dumps(n.right, sort_keys=True) not in originals for n in neg)


def test_too_little_data_is_a_clear_error(tmp_path: Path) -> None:
    s = tmp_settings(tmp_path)
    with pytest.raises(ValueError, match="need >= 5 examples"):
        tr.fit(toy_pairs(4), s)


# --- active learning ----------------------------------------------------------------------


def test_pairs_from_resolved_label_rows() -> None:
    row = {"id": 9, "label": "match", "left_record": {"name": "Sharma Logistics"},
           "right_record": {"chosen": "U1", "candidates": [
               {"id": "U1", "name": "SHARMA LOGISTICS PRIVATE LIMITED", "features": {}},
               {"id": "U2", "name": "SHARMA LOGISTICS LIMITED"}]}}  # fmt: skip
    pairs = tr.pairs_from_label(row)
    assert [(p.right["name"], p.label) for p in pairs] == [
        ("SHARMA LOGISTICS PRIVATE LIMITED", 1), ("SHARMA LOGISTICS LIMITED", 0)]  # fmt: skip
    assert "features" not in pairs[0].right
    single = tr.pairs_from_label({"id": 3, "label": "0", "left_record": {"name": "a"},
                                  "right_record": {"name": "b"}})  # fmt: skip
    assert single == [tr.Pair({"name": "a"}, {"name": "b"}, 0, "match_labels:3")]


def test_append_pairs_skips_rows_already_added(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "p.csv", toy_pairs(3))  # hand-made file: no source column
    new = [tr.Pair({"name": "a"}, {"name": "b"}, 1, "match_labels:1")]
    assert tr.append_pairs(path, new) == 1
    assert tr.append_pairs(path, new) == 0
    rows = tr.load_pairs(path)
    assert len(rows) == 7 and rows[-1].source == "match_labels:1"
    assert rows[0].source == tr.GOLDEN


def test_cli_prints_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    cfg = tmp_path / "config.yaml"
    text = DEFAULT_CONFIG_PATH.read_text().replace(
        "model_path: models/matcher.joblib", f"model_path: {tmp_path / 'm.joblib'}"
    )
    cfg.write_text(text)
    monkeypatch.setenv("CONFIG_PATH", str(cfg))
    get_settings.cache_clear()
    try:
        csv_path = write_csv(tmp_path / "pairs.csv", toy_pairs())
        assert tr.main(["--csv", str(csv_path), "--no-write-config"]) == 0
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert "precision" in out and "held for a person" in out
    assert cfg.read_text() == text  # --no-write-config


# --- ladder hook --------------------------------------------------------------------------

SHARMA = [
    mca("U63090MH2011PTC000001", "SHARMA LOGISTICS PRIVATE LIMITED", "Maharashtra"),
    mca("U63090DL2014PLC000002", "SHARMA LOGISTICS LIMITED", None),
]


def _fake_scores(monkeypatch: pytest.MonkeyPatch, by_state: dict[str, float]) -> None:
    """Score each candidate by its state, so the test controls the bands exactly."""
    monkeypatch.setattr(ladder, "features", lambda left, right: {"right": right, "state_match": 0,
                                                                 "pan_equal": 0})  # fmt: skip
    monkeypatch.setattr(ladder, "score", lambda f: by_state[f["right"]["state"] or "Delhi"])


@pytest.mark.parametrize(
    ("scores", "outcome", "reason_part"),
    [
        ({"Maharashtra": 0.97, "Delhi": 0.05}, Outcome.match, "scored matcher"),
        ({"Maharashtra": 0.97, "Delhi": 0.95}, Outcome.cannot_verify, "undecided"),
        ({"Maharashtra": 0.70, "Delhi": 0.05}, Outcome.cannot_verify, "undecided"),
        ({"Maharashtra": 0.05, "Delhi": 0.05}, Outcome.no_match, NO_ENTITY),
    ],
)
async def test_matcher_settles_what_rules_cannot(
    monkeypatch: pytest.MonkeyPatch, scores: dict, outcome: Outcome, reason_part: str
) -> None:
    _fake_scores(monkeypatch, scores)
    reg = FakeRegistry(search={"Sharma Logistics Pvt Ltd": SHARMA})
    acct = AccountIn.from_row({"id": "a", "name": "Sharma Logistics Pvt Ltd", "data": {}})
    deps = IdentityDeps(fetch=FakeFetch({}), registry=reg, save=FakeSave(),
                        crosswalk=lambda p: None)  # fmt: skip
    r = await identify(acct, "job", deps, S)  # rules alone: SEVERAL (no HQ state)
    assert r.outcome is outcome and reason_part in r.reason
    if outcome is Outcome.match:
        assert r.cin.value == "U63090MH2011PTC000001"


async def test_without_a_model_rules_stand(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ladder, "score", lambda f: None)
    reg = FakeRegistry(search={"Sharma Logistics Pvt Ltd": SHARMA})
    acct = AccountIn.from_row({"id": "a", "name": "Sharma Logistics Pvt Ltd", "data": {}})
    deps = IdentityDeps(fetch=FakeFetch({}), registry=reg, save=FakeSave(),
                        crosswalk=lambda p: None)  # fmt: skip
    r = await identify(acct, "job", deps, S)
    assert r.outcome is Outcome.cannot_verify and r.reason == SEVERAL
