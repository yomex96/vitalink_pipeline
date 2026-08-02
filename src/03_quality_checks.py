# 03_quality_checks.py
"""
Data Quality stage: implements the "Before Storage" and "Before
Serving" validation checkpoints from the proposal (Section 3):
deduplication/referential integrity checks and entity-resolution
confidence scoring, expanded here into a small set of concrete checks
across completeness, accuracy, and consistency dimensions.
"""

import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "db" / "vitalink.db"


def run_quality_checks():
    print("Running Data Quality & Validation Checks...")
    conn = sqlite3.connect(DB_PATH)
    issues = []

    try:
        dim_patients = pd.read_sql("SELECT * FROM dim_patients", conn)
        fact_labs = pd.read_sql("SELECT * FROM fact_lab_results", conn)
        stg_patients = pd.read_sql("SELECT * FROM stg_ehr_patients", conn)
        review_queue = pd.read_sql("SELECT * FROM entity_resolution_review_queue", conn)

        # --- Completeness ---
        total_staged = len(stg_patients)
        total_master = dim_patients["enterprise_patient_id"].nunique()
        print(f"  Completeness: {total_staged} staged patients -> {total_master} master profiles")

        # --- Referential integrity: orphaned lab results ---
        orphans = fact_labs["enterprise_patient_id"].isna().sum()
        print(f"  Referential integrity: {orphans} orphaned lab record(s) with no matching patient")
        if orphans > 0:
            issues.append(f"{orphans} lab result(s) reference a local_patient_id not found in the patient source")

        # --- Accuracy: implausible / future DOBs ---
        dob_dates = pd.to_datetime(dim_patients["dob"], errors="coerce")
        today = pd.Timestamp(date.today())
        future_dob = (dob_dates > today).sum()
        very_old = (dob_dates < pd.Timestamp("1900-01-01")).sum()
        print(f"  Accuracy: {future_dob} future DOB(s), {very_old} implausible DOB(s) before 1900")
        if future_dob > 0:
            issues.append(f"{future_dob} patient(s) have a date of birth in the future")

        # --- Accuracy: duplicate SSNs not merged by entity resolution ---
        ssn_counts = dim_patients.groupby("ssn")["enterprise_patient_id"].nunique()
        unresolved_ssn_dupes = (ssn_counts > 1).sum()
        print(f"  Accuracy: {unresolved_ssn_dupes} SSN(s) still split across multiple master profiles")
        if unresolved_ssn_dupes > 0:
            issues.append(f"{unresolved_ssn_dupes} SSN(s) appear under more than one enterprise_patient_id")

        # --- Consistency: lab result values within a sane numeric range ---
        negative_results = (fact_labs["result_value"] < 0).sum()
        print(f"  Consistency: {negative_results} lab result(s) with a negative value")
        if negative_results > 0:
            issues.append(f"{negative_results} lab result(s) have a negative result_value")

        # --- Entity resolution confidence: size of manual review queue ---
        print(f"  Entity resolution: {len(review_queue)} match pair(s) awaiting manual review")

        # --- Cross-source resolution (pharmacy): completeness + confidence ---
        fact_rx = pd.read_sql("SELECT * FROM fact_pharmacy_fulfillment", conn)
        rx_review_queue = pd.read_sql("SELECT * FROM pharmacy_entity_resolution_review_queue", conn)
        rx_total = len(fact_rx)
        rx_pending = fact_rx["enterprise_patient_id"].isna().sum()
        rx_matched_existing = fact_rx["enterprise_patient_id"].isin(dim_patients["enterprise_patient_id"]).sum()
        rx_new_patients = rx_total - rx_pending - rx_matched_existing
        print(f"  Cross-source (pharmacy): {rx_total} records, {rx_matched_existing} matched to "
              f"existing EHR patients, {rx_new_patients} patient(s) identified only via pharmacy "
              f"(retained, not dropped), {len(rx_review_queue)} pending manual review")

        print()
        if not issues:
            print("All Data Quality checks passed!")
        else:
            print(f"{len(issues)} data quality issue(s) detected:")
            for issue in issues:
                print(f"    - {issue}")
            print("  (Per proposal error strategy: <5% failure rate is quarantined and logged; "
                  "the pipeline halts only if failures exceed 5% of records.)")
    finally:
        conn.close()


if __name__ == "__main__":
    run_quality_checks()
