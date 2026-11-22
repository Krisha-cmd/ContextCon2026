# Golden data

## match_pairs.csv

This file holds hand-labelled entity pairs for the identity matcher (`app/identity/train.py`).

| column | content |
|---|---|
| `left` | JSON object for our account, e.g. `{"name": "Sharma Logistics Pvt Ltd", "state": "Maharashtra", "city": "Mumbai", "domain": "sharmalogistics.in"}` |
| `right` | JSON object for the registry record or other account, e.g. `{"name": "SHARMA LOGISTICS PRIVATE LIMITED", "state": "Maharashtra", "email": "info@sharmalogistics.in", "pan": "AAPCS1234B"}` |
| `label` | `1` if both are the same legal entity, `0` if not |
| `source` | optional. Defaults to `golden`. Rows appended from `match_labels` carry `match_labels:<id>`. |

Each record can use these keys: `name`, `state`, `city`, `domain`, `email`, `pan`, `gstin` and `directors` (a list of names). Leave a key out when you don't know its value. Missing keys count as "no evidence", never as a mismatch. A bare name in a cell, instead of a JSON object, also works.

Calibration needs at least 5 pairs of each label in the training split. Aim for at least 20 of each.

To train the model and write `matching.match_threshold` back to `config.yaml`:

```
uv run python -m app.identity.train
uv run python -m app.identity.train --from-labels   # first append resolved match_labels rows (needs the DB)
```
