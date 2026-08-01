# VitaLink Pipeline

A prototype data engineering pipeline for **VitaLink Health Network**, built for DSIO2010 Final Project (Option A: Working Prototype).

This prototype implements a scaled-down version of the Preventive Care Gap Detection Pipeline described in the [project proposal](#relationship-to-proposal): it ingests two patient-level data sources, resolves duplicate patient identities, runs data quality checks, and produces a summary analytics report.

## Overview

| | |
|---|---|
| **Sources ingested** | EHR patient demographics, lab results |
| **Storage** | SQLite (`db/vitalink.db`) |
| **Entity resolution** | Blocking + deterministic/probabilistic (RapidFuzz) matching |
| **Orchestration** | `run_pipeline.py` runs all four stages in sequence |

## Setup Instructions

**Requirements:** Python 3.9+

```bash
# from the project root
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Place source files in `data/`:
- `data/source1_ehr_patients.csv` — columns: `local_id, first_name, last_name, dob, zip_code, ssn`
- `data/source2_lab_results.csv` — columns: `lab_id, local_patient_id, test_code, result_value, result_date`

## Running the Pipeline

```bash
python3 run_pipeline.py
```

This runs all four stages in order. Each can also be run independently (later stages depend on the SQLite tables created by earlier ones):

```bash
python3 src/01_ingest.py              # -> stg_ehr_patients, stg_lab_results
python3 src/02_entity_resolution.py   # -> dim_patients, fact_lab_results, entity_resolution_review_queue
python3 src/03_quality_checks.py      # reads the above, prints a QA report
python3 src/04_analytics.py           # reads the above, prints summary analytics
```

## Troubleshooting

**`ModuleNotFoundError: No module named 'pandas'`**

This means the script is running with your system Python rather than the virtual environment's Python — usually because the venv was created but never activated (or was activated in a different terminal session).

If you already have a `venv/` folder for this project, just activate it before running:

```bash
source venv/bin/activate        # Windows: venv\Scripts\activate
python3 run_pipeline.py
```

If you don't have one yet:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 run_pipeline.py
```

Your terminal prompt should show `(venv)` at the start once it's active — if it doesn't, the activation didn't take and the error will recur.

## Architecture

```
source1_ehr_patients.csv ─┐
                           ├─> 01_ingest.py ──> stg_ehr_patients
source2_lab_results.csv ──┘                     stg_lab_results
                                                       │
                                                       ▼
                                        02_entity_resolution.py
                                    (standardize → block → match)
                                                       │
                                    ┌──────────────────┼──────────────────┐
                                    ▼                                     ▼
                              dim_patients                    entity_resolution_review_queue
                            fact_lab_results
                                    │
                                    ▼
                        03_quality_checks.py  (completeness, accuracy,
                                                referential integrity, consistency)
                                    │
                                    ▼
                            04_analytics.py  (summary report)
```

### Data Ingestion (`01_ingest.py`)

Reads both CSVs, enforces a required-column schema check, and standardizes each field:
- **Dates of birth / result dates**: source data mixes formats (`1/29/2021`, `2009-12-20`, `09/27/1949`); parsed with pandas' mixed-format parser and normalized to `YYYY-MM-DD`.
- **ZIP codes**: the raw export stores ZIPs as numbers, which drops leading zeros (`02108` → `2108`). Re-padded to 5 digits.
- **Result values**: coerced to numeric; non-numeric values are quarantined.

Records missing a required field (ID, name, valid DOB for patients; valid result value/date for labs) are quarantined — dropped from the staged table and counted — rather than silently kept or crashing the run. If the quarantine rate exceeds 5% of a file, a warning is printed (mirroring the proposal's error-handling strategy in Section 3, scaled down since this prototype doesn't halt the whole pipeline on dev data).

### Entity Resolution (`02_entity_resolution.py`)

**Note on scope:** the full proposal (Section 2) describes resolving patient identity across *five* sources, each with its own local ID space (Epic MRN, Athena ID, lab vendor ID, etc.). In this two-source prototype, lab records already carry the same `local_id` used by the EHR source — so joining labs to patients is a direct key lookup, not a cross-source resolution problem. The realistic entity-resolution problem present in *this* data is **duplicate patient entries within the EHR source** (e.g. the same person re-entered with a typo'd name), which is what this stage solves, using a scaled-down version of the proposal's design:

1. **Standardization** — names lowercased and whitespace-trimmed.
2. **Blocking** — patients are only compared within the same birth year *and* the same 3-digit ZIP prefix, avoiding an O(n²) full comparison (per proposal's blocking strategy).
3. **Deterministic tier** — exact SSN match, or exact standardized name + DOB match, auto-links two records.
4. **Probabilistic tier** — for records in the same block with an identical DOB, RapidFuzz's `fuzz.ratio` scores name similarity:
   - **≥ 90** → auto-linked as the same person
   - **75–89** → written to `entity_resolution_review_queue` for manual review (mirrors the proposal's 0.70–0.85 human-review band, rescaled to RapidFuzz's 0–100 score)
   - **< 75** → treated as distinct individuals
5. Matched records are grouped via union-find, and each group is assigned one `enterprise_patient_id` (`EPI_xxxxxxxx`).

**Trade-off:** thresholds (90 / 75) were set conservatively to favor precision over recall — i.e., avoid merging two different people — at the cost of occasionally leaving a genuine duplicate unmerged if it doesn't share an exact DOB. This matches the proposal's stated priority of keeping matching precision above 98%, even at the expense of catching every duplicate automatically.

### Data Quality (`03_quality_checks.py`)

Implements the "Before Storage" / "Before Serving" checkpoints from the proposal (Section 3):

| Dimension | Check |
|---|---|
| Completeness | staged patient count vs. resolved master profile count |
| Referential integrity | lab results with no matching `enterprise_patient_id` (orphans) |
| Accuracy | future or implausible (pre-1900) dates of birth |
| Accuracy | SSNs still split across more than one master profile (missed ER merges) |
| Consistency | negative lab result values |
| ER confidence | size of the manual review queue |

### Analytics (`04_analytics.py`)

Produces a "Top 10 Patients by Lab Activity" summary (test count, first/last lab date) as a lightweight stand-in for the proposal's dashboard, plus a count of entity-resolution pairs still pending manual review.

## Database Schema

| Table | Description |
|---|---|
| `stg_ehr_patients` | Cleaned, staged patient records (pre-resolution) |
| `stg_lab_results` | Cleaned, staged lab results (pre-resolution) |
| `dim_patients` | `stg_ehr_patients` + resolved `enterprise_patient_id` |
| `fact_lab_results` | `stg_lab_results` + resolved `enterprise_patient_id` |
| `entity_resolution_review_queue` | Record pairs scoring 75–89 similarity, awaiting human review |

## Relationship to Proposal

This prototype demonstrates core capabilities at a reduced scale appropriate for the assignment (2 of the proposal's 5 sources; SQLite instead of PostgreSQL). Key differences and why:

- **Sources**: proposal specifies 5 (EHR, labs, scheduling, protocol reference, pharmacy); prototype implements 2 (EHR, labs) to demonstrate the ingestion → resolution → quality → analytics pattern without requiring live API/DB integrations for a class project.
- **Entity resolution problem**: proposal resolves identity *across* disparate source ID spaces; this dataset's lab source already shares the EHR's ID space, so the prototype's ER instead resolves *duplicate records within* the EHR source — using the same blocking + deterministic/probabilistic design, just applied to the duplicate-detection version of the problem.
- **Storage**: proposal specifies PostgreSQL for production-scale operational storage; prototype uses SQLite for zero-setup local development.

## Known Limitations

- Blocking key (birth year + ZIP prefix) means two duplicate records with a data-entry error in *either* field won't be compared at all — a known precision/recall trade-off of blocking, called out as a risk in the proposal.
- No automated feedback loop yet for the manual review queue (listed as future work in the proposal's Section 5).
- SSNs in the source data are synthetic/randomly generated per record and don't reliably indicate duplicates on their own; they're used only as one signal alongside name+DOB matching.
