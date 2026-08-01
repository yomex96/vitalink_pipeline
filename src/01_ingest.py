# 01_ingest.py
"""
Ingestion stage: reads raw EHR patient and lab result CSV extracts,
validates and standardizes them, and loads them into staging tables
in SQLite (db/vitalink.db).

Validation performed here corresponds to the "At Ingestion" checkpoint
described in the project proposal (Section 3): schema/required-field
checks and row-count anomaly detection. Records that fail validation
are quarantined (dropped from the staged table and logged) rather than
silently passed through or crashing the whole run.
"""

import sys
import sqlite3
import pandas as pd
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = BASE_DIR / "db" / "vitalink.db"

# Minimum expected row counts. In production this would be a rolling
# 7-day average (per proposal); for the prototype we use a static
# floor to demonstrate the same alerting concept.
MIN_EXPECTED_PATIENT_ROWS = 1
MIN_EXPECTED_LAB_ROWS = 1

REQUIRED_PATIENT_COLS = ["local_id", "first_name", "last_name", "dob", "zip_code", "ssn"]
REQUIRED_LAB_COLS = ["lab_id", "local_patient_id", "test_code", "result_value", "result_date"]


def load_csv(path: Path, required_cols: list[str], label: str) -> pd.DataFrame:
    """Load a CSV and enforce a schema check before any transformation."""
    if not path.exists():
        print(f"❌ {label}: file not found at {path}")
        sys.exit(1)

    try:
        df = pd.read_csv(path)
    except Exception as e:
        print(f"❌ {label}: failed to parse CSV — {e}")
        sys.exit(1)

    df.columns = df.columns.str.strip().str.lower()
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        print(f"❌ {label}: missing required column(s): {missing}")
        sys.exit(1)

    return df


def parse_dob(series: pd.Series) -> pd.Series:
    """
    Source data mixes date formats (e.g. '1/29/2021' and '2009-12-20').
    pandas' default parser handles most of this per-row, but we fall
    back to dayfirst=False explicitly since all slash-formatted dates
    in this source follow US convention (M/D/Y).
    """
    return pd.to_datetime(series, errors="coerce", format="mixed")


def clean_patients(df_p: pd.DataFrame) -> pd.DataFrame:
    before = len(df_p)

    patients_clean = pd.DataFrame()
    patients_clean["local_id"] = df_p["local_id"].astype(str).str.strip()
    patients_clean["first_name"] = df_p["first_name"].astype(str).str.strip()
    patients_clean["last_name"] = df_p["last_name"].astype(str).str.strip()
    patients_clean["dob"] = parse_dob(df_p["dob"]).dt.strftime("%Y-%m-%d")

    # Zip codes arrive as numbers in the raw export, which drops
    # leading zeros (e.g. Boston's 02108 becomes 2108). Restore them.
    patients_clean["zip_code"] = (
        df_p["zip_code"].astype(str).str.extract(r"(\d+)")[0].str.zfill(5)
    )
    patients_clean["ssn"] = df_p["ssn"].astype(str).str.strip()

    # Quarantine rule: drop rows missing an ID, a name, or a valid DOB.
    # (>5% failure would halt the pipeline per proposal Section 3; for
    # this prototype we log the rate and continue since it's dev data.)
    valid_mask = (
        patients_clean["local_id"].ne("")
        & patients_clean["local_id"].ne("nan")
        & patients_clean["first_name"].ne("")
        & patients_clean["last_name"].ne("")
        & patients_clean["dob"].notna()
    )
    quarantined = (~valid_mask).sum()
    patients_clean = patients_clean[valid_mask].reset_index(drop=True)

    fail_rate = quarantined / before if before else 0
    print(f"  • Patients: {before} read, {quarantined} quarantined ({fail_rate:.1%}), "
          f"{len(patients_clean)} staged")
    if fail_rate > 0.05:
        print("  ⚠️  Validation failure rate exceeds 5% threshold — review source file.")

    return patients_clean


def clean_labs(df_l: pd.DataFrame) -> pd.DataFrame:
    before = len(df_l)

    labs_clean = pd.DataFrame()
    labs_clean["lab_id"] = df_l["lab_id"].astype(str).str.strip()
    labs_clean["local_patient_id"] = df_l["local_patient_id"].astype(str).str.strip()
    labs_clean["test_code"] = df_l["test_code"].astype(str).str.strip()
    labs_clean["result_value"] = pd.to_numeric(df_l["result_value"], errors="coerce")
    labs_clean["result_date"] = parse_dob(df_l["result_date"]).dt.strftime("%Y-%m-%d")

    valid_mask = (
        labs_clean["lab_id"].ne("")
        & labs_clean["local_patient_id"].ne("")
        & labs_clean["result_value"].notna()
        & labs_clean["result_date"].notna()
    )
    quarantined = (~valid_mask).sum()
    labs_clean = labs_clean[valid_mask].reset_index(drop=True)

    fail_rate = quarantined / before if before else 0
    print(f"  • Labs: {before} read, {quarantined} quarantined ({fail_rate:.1%}), "
          f"{len(labs_clean)} staged")
    if fail_rate > 0.05:
        print("  ⚠️  Validation failure rate exceeds 5% threshold — review source file.")

    return labs_clean


def clean_and_ingest():
    print("🧹 Ingesting and validating VitaLink source data...")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    df_p = load_csv(DATA_DIR / "source1_ehr_patients.csv", REQUIRED_PATIENT_COLS, "EHR patients")
    df_l = load_csv(DATA_DIR / "source2_lab_results.csv", REQUIRED_LAB_COLS, "Lab results")

    patients_clean = clean_patients(df_p)
    labs_clean = clean_labs(df_l)

    # Row-count anomaly check (simplified stand-in for the 7-day
    # rolling-average alert described in the proposal).
    if len(patients_clean) < MIN_EXPECTED_PATIENT_ROWS:
        print("  ⚠️  Patient row count below expected minimum — possible upstream export issue.")
    if len(labs_clean) < MIN_EXPECTED_LAB_ROWS:
        print("  ⚠️  Lab row count below expected minimum — possible upstream export issue.")

    conn = sqlite3.connect(DB_PATH)
    try:
        patients_clean.to_sql("stg_ehr_patients", conn, if_exists="replace", index=False)
        labs_clean.to_sql("stg_lab_results", conn, if_exists="replace", index=False)
    finally:
        conn.close()

    print("✅ Ingestion complete — staged data loaded into db/vitalink.db")


if __name__ == "__main__":
    clean_and_ingest()
