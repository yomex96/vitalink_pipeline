# VitaLink Pipeline

A prototype data engineering pipeline for **VitaLink Health Network**, built for DSIO2010 Final Project (Option A: Working Prototype).

This prototype implements a scaled-down version of the Preventive Care Gap Detection Pipeline described in the [project proposal](#relationship-to-proposal): it ingests two patient-level data sources, resolves duplicate patient identities, runs data quality checks, and produces a summary analytics report.

## Overview

| | |
|---|---|
| **Sources ingested** | EHR patient demographics, lab results, pharmacy fulfillment logs |
| **Storage** | SQLite (`db/vitalink.db`) |
| **Entity resolution** | Blocking + deterministic/probabilistic (RapidFuzz) matching |
| **Orchestration** | `run_pipeline.py` runs all four stages in sequence |

## Repository Structure

```
vitalink_pipeline/
├── data/
│   ├── source1_ehr_patients.csv       EHR patient demographics
│   ├── source2_lab_results.csv        Lab results (shares EHR's local_id)
│   └── source3_pharmacy_logs.csv      Pharmacy logs (no shared ID — name/DOB only)
├── db/
│   ├── schema.sql                     Reference schema + sample queries
│   └── vitalink.db                    (generated automatically on run)
├── src/
│   ├── 01_ingest.py                   Validation, cleaning, quarantine
│   ├── 02_entity_resolution.py        Within-EHR dedup + cross-source pharmacy matching
│   ├── 03_quality_checks.py           Completeness, accuracy, referential integrity
│   └── 04_analytics.py                Summary report
├── venv/                              (created during setup — not tracked in git)
├── README.md
├── requirements.txt
└── run_pipeline.py                    Runs all four stages in sequence
```

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
- `data/source3_pharmacy_logs.csv` — columns: `pharmacy_record_id, patient_first_name, patient_last_name, patient_dob, medication, fill_date` (deliberately has **no** `local_id` — see Entity Resolution below)

## Running the Pipeline

```bash
python3 run_pipeline.py
```

This runs all four stages in order. Each can also be run independently (later stages depend on the SQLite tables created by earlier ones):

```bash
python3 src/01_ingest.py              # -> stg_ehr_patients, stg_lab_results, stg_pharmacy_logs
python3 src/02_entity_resolution.py   # -> dim_patients, fact_lab_results, entity_resolution_review_queue,
                                       #    fact_pharmacy_fulfillment, pharmacy_entity_resolution_review_queue
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
source3_pharmacy_logs.csv ────────────────────> stg_pharmacy_logs
                                                       │
                                                       ▼
                                    02_entity_resolution.py
                              (EHR: standardize → block → match)
                                                       │
                                    ┌──────────────────┼──────────────────┐
                                    ▼                                     ▼
                              dim_patients                    entity_resolution_review_queue
                            fact_lab_results                              │
                                    │                                     │
                                    ▼                                     │
                    match_pharmacy_to_master()                           │
              (cross-namespace: name+DOB only, no shared ID)             │
                                    │                                     │
                    ┌───────────────┼───────────────┐                    │
                    ▼                                ▼                   │
        fact_pharmacy_fulfillment      pharmacy_entity_resolution_review_queue
                    │                                                    │
                    └────────────────────┬───────────────────────────────┘
                                          ▼
                        03_quality_checks.py  (completeness, accuracy,
                                referential integrity, consistency,
                                cross-source match confidence)
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

**Note on scope:** the full proposal (Section 2) describes resolving patient identity across *five* sources, each with its own local ID space (Epic MRN, Athena ID, lab vendor ID, etc.). This prototype implements two different flavors of that problem, both real:

1. **Lab records** already carry the same `local_id` used by the EHR source, so joining labs to patients is a direct key lookup. Within *this* source pairing, the realistic entity-resolution problem is **duplicate patient entries within the EHR source itself** (e.g. the same person re-entered with a typo'd name) — solved by the within-source matching described below.
2. **Pharmacy records** (`source3_pharmacy_logs.csv`) carry **no identifier shared with the EHR source at all** — only a patient name and date of birth, matching how a real pharmacy vendor's system would have no knowledge of a hospital's internal patient IDs. Resolving these against the master patient index is genuine cross-namespace entity resolution — the core problem the proposal describes — and is handled separately in `match_pharmacy_to_master()`, documented after the EHR-internal matching below.

Both use a scaled-down version of the proposal's design:

1. **Standardization** — names lowercased and whitespace-trimmed.
2. **Blocking** — patients are only compared within the same birth year *and* the same 3-digit ZIP prefix, avoiding an O(n²) full comparison (per proposal's blocking strategy).
3. **Deterministic tier** — exact SSN match, or exact standardized name + DOB match, auto-links two records.
4. **Probabilistic tier** — for records in the same block with an identical DOB, RapidFuzz's `fuzz.ratio` scores name similarity:
   - **≥ 90** → auto-linked as the same person
   - **75–89** → written to `entity_resolution_review_queue` for manual review (mirrors the proposal's 0.70–0.85 human-review band, rescaled to RapidFuzz's 0–100 score)
   - **< 75** → treated as distinct individuals
5. Matched records are grouped via union-find, and each group is assigned one `enterprise_patient_id` (`EPI_xxxxxxxx`).

**Trade-off:** thresholds (90 / 75) were set conservatively to favor precision over recall — i.e., avoid merging two different people — at the cost of occasionally leaving a genuine duplicate unmerged if it doesn't share an exact DOB. This matches the proposal's stated priority of keeping matching precision above 98%, even at the expense of catching every duplicate automatically.

#### Cross-source resolution: pharmacy (`match_pharmacy_to_master()`)

Pharmacy records have no ID in common with the EHR/master patient index — only a name and DOB — so this function matches each pharmacy record against the **already-resolved** `dim_patients` table rather than against other pharmacy records:

1. **Block** by birth year only (no ZIP is available on the pharmacy side).
2. **Deterministic tier** — exact standardized name + DOB match against a master patient auto-links.
3. **Probabilistic tier** — same DOB, RapidFuzz name score ≥90 auto-links; 75–89 goes to `pharmacy_entity_resolution_review_queue`.
4. **No match found** — rather than dropping the record or treating "no local_id" as an error, the pipeline mints a **new** `enterprise_patient_id` for a patient known only through the pharmacy source. This directly implements the proposal's bias-mitigation commitment (Section 4): excluding patients who don't appear in the EHR would systematically under-detect care gaps for exactly the population (uninsured, transient, walk-in-only patients) the proposal identifies as most at risk of being missed.

On the test data, this correctly matched typo'd cross-source variants (e.g. a pharmacy record for "Paul King" matched to EHR patient "Paul Kig") while also correctly retaining patients who only appear in the pharmacy log rather than silently dropping them.

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

Produces a "Top 10 Patients by Lab Activity" summary (test count, first/last lab date) as a lightweight stand-in for the proposal's dashboard, plus counts of entity-resolution pairs still pending manual review (both within-EHR and cross-source pharmacy) and patients identified only through the pharmacy source.

## Database Schema

| Table | Description |
|---|---|
| `stg_ehr_patients` | Cleaned, staged patient records (pre-resolution) |
| `stg_lab_results` | Cleaned, staged lab results (pre-resolution) |
| `stg_pharmacy_logs` | Cleaned, staged pharmacy records (pre-resolution) — no shared ID with EHR |
| `dim_patients` | `stg_ehr_patients` + resolved `enterprise_patient_id` |
| `fact_lab_results` | `stg_lab_results` + resolved `enterprise_patient_id` |
| `entity_resolution_review_queue` | Within-EHR duplicate pairs scoring 75–89 similarity, awaiting human review |
| `fact_pharmacy_fulfillment` | `stg_pharmacy_logs` + resolved `enterprise_patient_id` (matched, newly minted, or pending review) |
| `pharmacy_entity_resolution_review_queue` | Cross-source pharmacy-to-master candidate matches scoring 75–89, awaiting human review |

## Relationship to Proposal

This prototype demonstrates core capabilities at a reduced scale appropriate for the assignment (3 of the proposal's 5 sources; SQLite instead of PostgreSQL). Key differences and why:

- **Sources**: proposal specifies 5 (EHR, labs, scheduling, protocol reference, pharmacy); prototype implements 3 (EHR, labs, pharmacy) to demonstrate the ingestion → resolution → quality → analytics pattern, including both same-namespace joins and genuine cross-namespace resolution, without requiring live API/DB integrations for a class project.
- **Entity resolution problem**: the proposal's central claim is resolving identity *across* disparate source ID spaces. The lab source in this dataset already shares the EHR's ID space, so that pairing instead demonstrates *duplicate-detection within* the EHR source. The pharmacy source was added specifically to demonstrate the proposal's actual core use case: it carries no shared identifier at all, so matching it against the master patient index is real cross-namespace entity resolution, using the same blocking + deterministic/probabilistic design.
- **Storage**: proposal specifies PostgreSQL for production-scale operational storage; prototype uses SQLite for zero-setup local development.

## Known Limitations

- Blocking key (birth year + ZIP prefix) means two duplicate records with a data-entry error in *either* field won't be compared at all — a known precision/recall trade-off of blocking, called out as a risk in the proposal.
- No automated feedback loop yet for the manual review queue (listed as future work in the proposal's Section 5).
- SSNs in the source data are synthetic/randomly generated per record and don't reliably indicate duplicates on their own; they're used only as one signal alongside name+DOB matching.
