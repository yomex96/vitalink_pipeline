# 04_analytics.py
"""
Analytics stage: produces summary views over the resolved,
quality-checked data — a lightweight stand-in for the dashboard
described in the proposal (Section 2).
"""

import sqlite3
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "db" / "vitalink.db"


def generate_reports():
    conn = sqlite3.connect(DB_PATH)
    try:
        top_activity = pd.read_sql("""
            SELECT
                p.enterprise_patient_id,
                p.first_name,
                p.last_name,
                COUNT(l.lab_id) AS total_lab_tests,
                MIN(l.result_date) AS first_lab_date,
                MAX(l.result_date) AS last_lab_date
            FROM dim_patients p
            LEFT JOIN fact_lab_results l ON p.enterprise_patient_id = l.enterprise_patient_id
            GROUP BY p.enterprise_patient_id
            ORDER BY total_lab_tests DESC
            LIMIT 10
        """, conn)

        print("Top 10 Patients by Lab Activity:")
        print(top_activity.to_string(index=False))

        review_summary = pd.read_sql(
            "SELECT COUNT(*) AS pending_review_pairs FROM entity_resolution_review_queue", conn
        )
        print(f"\nEntity resolution pairs pending manual review: "
              f"{review_summary['pending_review_pairs'][0]}")
    finally:
        conn.close()


if __name__ == "__main__":
    generate_reports()
