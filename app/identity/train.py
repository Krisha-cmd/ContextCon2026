"""Train the identity matcher: `python -m app.identity.train [--from-labels]`.

Data: tests/golden/match_pairs.csv (columns left, right, label[, source]; left/right are JSON
records like {"name": ..., "state": ..., "city": ...}; label 1 = same entity, 0 = not).
Hand-labelled pairs are split into train / held-out. Synthetic negatives are made only from
training positives, so the held-out set stays real and the threshold isn't flattered.

Model: LogisticRegression(class_weight="balanced") calibrated with isotonic regression
(CalibratedClassifierCV, cv=config.matching.calibration_cv), saved to matching.model_path.
match_threshold = the lowest p with 100% precision on the held-out pairs, written back to
config.yaml with a dated comment.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import joblib
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split

from app.config import DEFAULT_CONFIG_PATH, ROOT, Settings, get_settings
from app.identity.matcher import FEATURES, Band, features, model_path, vector
from app.log import get_logger

log = get_logger(__name__)

CSV_FIELDS = ("left", "right", "label", "source")
GOLDEN = "golden"
NEVER = 1.01  # a match_threshold no probability reaches: auto-match off
_THRESHOLD_LINE = re.compile(r"^(?P<indent>[ \t]+)match_threshold:.*$", re.MULTILINE)
RESOLVED_LABELS = {"1": 1, "match": 1, "0": 0, "no_match": 0}


@dataclass(frozen=True)
class Pair:
    left: dict[str, Any]
    right: dict[str, Any]
    label: int
    source: str = GOLDEN


# ---- data -------------------------------------------------------------------------------
def _record(cell: str) -> dict[str, Any]:
    """A JSON object, or a bare name."""
    cell = (cell or "").strip()
    if cell.startswith("{"):
        return json.loads(cell)
    return {"name": cell}


def load_pairs(path: Path) -> list[Pair]:
    with path.open(newline="", encoding="utf-8") as fh:
        return [
            Pair(
                _record(r["left"]),
                _record(r["right"]),
                int(r["label"]),
                (r.get("source") or GOLDEN).strip(),
            )
            for r in csv.DictReader(fh)
        ]


def _header(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as fh:
        return next(csv.reader(fh), [])


def _write(path: Path, pairs: Iterable[Pair], mode: str) -> None:
    with path.open(mode, newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        if mode == "w":
            w.writeheader()
        for p in pairs:
            w.writerow(
                {
                    "left": json.dumps(p.left, ensure_ascii=False),
                    "right": json.dumps(p.right, ensure_ascii=False),
                    "label": p.label,
                    "source": p.source,
                }
            )


def append_pairs(path: Path, pairs: Iterable[Pair]) -> int:
    """Append pairs whose source isn't in the file yet. Returns how many were written.
    A file without a `source` column is rewritten once with it (rows become 'golden')."""
    existing = load_pairs(path) if path.exists() else []
    have = {p.source for p in existing}
    new = [p for p in pairs if p.source not in have]
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or tuple(_header(path)) != CSV_FIELDS:
        _write(path, [*existing, *new], "w")
    else:
        _write(path, new, "a")
    return len(new)


# ---- synthetic negatives ----------------------------------------------------------------
def _swap_token(rec: dict, rng: random.Random, pool: Sequence[str], _: str) -> dict | None:
    tokens = (rec.get("name") or "").split()
    choices = [t for t in pool if t.lower() not in {x.lower() for x in tokens}]
    if not tokens or not choices:
        return None
    tokens[rng.randrange(len(tokens))] = rng.choice(choices)
    return {**rec, "name": " ".join(tokens)}


def _add_token(rec: dict, rng: random.Random, pool: Sequence[str], token: str) -> dict | None:
    name = rec.get("name") or ""
    if not name or token.lower() in name.lower().split():
        return None
    return {**rec, "name": f"{name} {token}"}


def _change_city(rec: dict, rng: random.Random, cities: Sequence[str], _: str) -> dict | None:
    others = [c for c in cities if c.lower() != (rec.get("city") or "").lower()]
    if not rec.get("city") or not others:
        return None  # an unknown city can't be "changed" into a real difference
    return {**rec, "city": rng.choice(others)}


PERTURBATIONS = (
    ("swap_token", _swap_token),
    ("add_token", _add_token),
    ("change_city", _change_city),
)


def synthetic_negatives(
    pairs: Sequence[Pair], per_positive: int, token: str, seed: int
) -> list[Pair]:
    """Perturb the right-hand record of each positive into a near-miss labelled 0."""
    rng = random.Random(seed)
    positives = [p for p in pairs if p.label == 1]
    name_pool = sorted({t for p in pairs for t in (p.right.get("name") or "").split()})
    city_pool = sorted({c for p in pairs for c in (p.left.get("city"), p.right.get("city")) if c})
    out = []
    for i, p in enumerate(positives):
        made = 0
        for k in range(len(PERTURBATIONS) * per_positive):
            if made >= per_positive:
                break
            kind, fn = PERTURBATIONS[(i + k) % len(PERTURBATIONS)]
            pool = city_pool if kind == "change_city" else name_pool
            right = fn(p.right, rng, pool, token)
            if right is None or right == p.right:
                continue
            if kind == "change_city" and not p.left.get("city"):
                continue
            out.append(Pair(p.left, right, 0, f"synthetic:{kind}:{i}:{k}"))
            made += 1
    return out


# ---- model ------------------------------------------------------------------------------
def xy(pairs: Sequence[Pair], s: Settings) -> tuple[list[list[float]], list[int]]:
    return [vector(features(p.left, p.right, s)) for p in pairs], [p.label for p in pairs]


def fit(pairs: Sequence[Pair], s: Settings) -> CalibratedClassifierCV:
    x, y = xy(pairs, s)
    m = s.matching
    smallest = min(y.count(0), y.count(1))
    if smallest < m.calibration_cv:
        raise ValueError(
            f"need >= {m.calibration_cv} examples of each label to calibrate with "
            f"cv={m.calibration_cv}; have {y.count(1)} positive, {y.count(0)} negative"
        )
    base = LogisticRegression(class_weight="balanced", max_iter=m.max_iter)
    model = CalibratedClassifierCV(base, method="isotonic", cv=m.calibration_cv)
    return model.fit(x, y)


def predict(model: Any, pairs: Sequence[Pair], s: Settings) -> list[float]:
    x, _ = xy(pairs, s)
    return [float(p) for p in model.predict_proba(x)[:, 1]] if x else []


def pick_match_threshold(scores: Sequence[float], labels: Sequence[int]) -> float | None:
    """Lowest p such that every held-out pair scoring >= p is a true match (and at least one
    does). None when even the top score is a non-match."""
    best = None
    for t in sorted(set(scores), reverse=True):
        chosen = [lab for p, lab in zip(scores, labels, strict=True) if p >= t]
        if not all(chosen):
            break
        best = t
    return best


def _band(p: float, match_t: float, nomatch_t: float) -> Band:
    if p >= match_t:
        return Band.match
    if p <= nomatch_t:
        return Band.no_match
    return Band.held


def band_report(
    scores: Sequence[float], labels: Sequence[int], match_t: float, nomatch_t: float
) -> dict[str, dict[str, float | int | None]]:
    """Per band: count, share, and precision/recall of the band's own claim (match bands
    claim 'same', no_match claims 'different'; held claims nothing)."""
    total = len(labels)
    pos, neg = sum(labels), total - sum(labels)
    out: dict[str, dict[str, float | int | None]] = {}
    for b in Band:
        got = [
            lab for p, lab in zip(scores, labels, strict=True) if _band(p, match_t, nomatch_t) is b
        ]
        n, n_pos = len(got), sum(got)
        right = n_pos if b is Band.match else (n - n_pos if b is Band.no_match else None)
        of = pos if b is Band.match else neg
        out[b.value] = {
            "n": n,
            "share": n / total if total else 0.0,
            "precision": (right / n if n else None) if right is not None else None,
            "recall": (right / of if of else None) if right is not None else None,
        }
    return out


# ---- config write-back ------------------------------------------------------------------
def write_threshold(config_path: Path, value: float, note: str) -> None:
    text = config_path.read_text(encoding="utf-8")
    matches = list(_THRESHOLD_LINE.finditer(text))
    if len(matches) != 1:
        found = len(matches)
        raise ValueError(f"expected one match_threshold line in {config_path}, found {found}")
    m = matches[0]
    line = f"{m.group('indent')}match_threshold: {value:.4f}  # {note}"
    config_path.write_text(text[: m.start()] + line + text[m.end() :], encoding="utf-8")


# ---- orchestration ----------------------------------------------------------------------
@dataclass
class TrainResult:
    match_threshold: float
    nomatch_threshold: float
    n_labelled: int
    n_train: int
    n_holdout: int
    n_synthetic: int
    report: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def train(
    s: Settings | None = None,
    *,
    csv_path: Path | None = None,
    config_path: Path | None = None,
    out_path: Path | None = None,
    write_config: bool = True,
    today: date | None = None,
) -> TrainResult:
    s = s or get_settings()
    m = s.matching
    path = csv_path or _abs(m.training_csv)
    labelled = load_pairs(path)
    labels = [p.label for p in labelled]
    train_pairs, holdout = train_test_split(
        labelled, test_size=m.holdout_fraction, random_state=m.random_seed, stratify=labels
    )
    synth = synthetic_negatives(
        train_pairs, m.synthetic_per_positive, m.synthetic_token, m.random_seed
    )
    model = fit([*train_pairs, *synth], s)
    scores = predict(model, holdout, s)
    held_labels = [p.label for p in holdout]
    picked = pick_match_threshold(scores, held_labels)
    warnings = []
    if picked is None:
        warnings.append("no threshold gives 100% precision on held-out pairs: auto-match off")
        threshold = NEVER
    elif picked <= m.nomatch_threshold:
        warnings.append(
            f"100%-precision threshold {picked:.4f} is at or below "
            f"nomatch_threshold {m.nomatch_threshold}; raised to it"
        )
        threshold = m.nomatch_threshold
    else:
        threshold = picked
    result = TrainResult(
        threshold,
        m.nomatch_threshold,
        len(labelled),
        len(train_pairs),
        len(holdout),
        len(synth),
        band_report(scores, held_labels, threshold, m.nomatch_threshold),
        warnings,
    )
    _save(model, out_path or model_path(s), result)
    if write_config:
        when = (today or date.today()).isoformat()
        note = (
            f"set {when} by app.identity.train: lowest p with 100% precision on "
            f"{len(holdout)} held-out pairs ({len(labelled)} labelled, {len(synth)} synthetic)"
        )
        write_threshold(config_path or DEFAULT_CONFIG_PATH, threshold, note)
        get_settings.cache_clear()
    return result


def _abs(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else ROOT / path


def _save(model: Any, path: Path, r: TrainResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "model": model,
            "features": FEATURES,
            "trained_at": datetime.now(UTC).isoformat(),
            "n_train": r.n_train,
            "n_holdout": r.n_holdout,
            "n_synthetic": r.n_synthetic,
            "match_threshold": r.match_threshold,
        },
        path,
    )


# ---- active learning --------------------------------------------------------------------
_RESOLVED = (
    "SELECT id, left_record, right_record, label FROM match_labels "
    "WHERE resolved_by <> 'pending' AND label = ANY(%s) ORDER BY id"
)


def pairs_from_label(row: dict[str, Any]) -> list[Pair]:
    """A resolved match_labels row -> training pairs.

    right_record is either one record, or (as identity writes it) {"candidates": [...]} with
    "chosen": <candidate id> when the person picked one. Every other candidate is a negative.
    """
    label = RESOLVED_LABELS[row["label"]]
    left, right = row["left_record"] or {}, row["right_record"] or {}
    src = f"match_labels:{row['id']}"
    cands = right.get("candidates")
    if not cands:
        return [Pair(left, right, label, src)]
    chosen = right.get("chosen") if label == 1 else None
    return [
        Pair(
            left,
            {k: v for k, v in c.items() if k != "features"},
            1 if chosen is not None and c.get("id") == chosen else 0,
            f"{src}:{c.get('id')}",
        )
        for c in cands
    ]


def retrain_from_labels(
    s: Settings | None = None, *, csv_path: Path | None = None, **train_kw: Any
) -> tuple[int, TrainResult]:
    """Append resolved match_labels rows to the training CSV, then retrain."""
    from app import db

    s = s or get_settings()
    path = csv_path or _abs(s.matching.training_csv)
    with db.tx() as conn:
        rows = conn.execute(_RESOLVED, (list(RESOLVED_LABELS),)).fetchall()
    added = append_pairs(path, [p for r in rows for p in pairs_from_label(r)])
    log.info("labels appended", extra={"rows": len(rows), "pairs_added": added})
    return added, train(s, csv_path=path, **train_kw)


# ---- CLI --------------------------------------------------------------------------------
def format_report(r: TrainResult) -> str:
    lines = [
        f"labelled pairs: {r.n_labelled}  (train {r.n_train}, held-out {r.n_holdout}); "
        f"synthetic negatives: {r.n_synthetic}",
        f"bands: match p >= {r.match_threshold:.4f}, no match p <= {r.nomatch_threshold:.4f}",
        "",
        f"{'band':<10}{'n':>5}{'share':>8}{'precision':>11}{'recall':>8}",
    ]
    for name, b in r.report.items():
        prec = "-" if b["precision"] is None else f"{b['precision']:.2f}"
        rec = "-" if b["recall"] is None else f"{b['recall']:.2f}"
        lines.append(f"{name:<10}{b['n']:>5}{b['share']:>8.0%}{prec:>11}{rec:>8}")
    lines.append(f"\nheld for a person: {r.report[Band.held.value]['share']:.0%} of held-out pairs")
    lines += [f"WARNING: {w}" for w in r.warnings]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.identity.train", description=__doc__)
    ap.add_argument(
        "--from-labels",
        action="store_true",
        help="first append resolved match_labels rows (needs DATABASE_URL)",
    )
    ap.add_argument("--csv", type=Path, help="training CSV (default: matching.training_csv)")
    ap.add_argument(
        "--no-write-config",
        action="store_true",
        help="don't write match_threshold back to config.yaml",
    )
    args = ap.parse_args(argv)
    kw = {"csv_path": args.csv, "write_config": not args.no_write_config}
    if args.from_labels:
        added, result = retrain_from_labels(**kw)
        print(f"appended {added} pairs from match_labels")
    else:
        result = train(**kw)
    print(format_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
