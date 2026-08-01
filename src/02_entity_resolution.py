# 02_entity_resolution.py
"""
Entity Resolution stage.

Simplification note: in this 2-source prototype, lab records already
carry the same local_id used by the EHR source (local_patient_id ==
local_id), so there is no cross-source ID-mapping problem to solve —
unlike the full proposal, where five sources each contribute their
own local key space. The realistic ER problem in *this* dataset is
detecting duplicate patient entries within the EHR source itself
(e.g. the same person re-entered with a typo'd name), so that's what
this stage resolves, using a scaled-down version of the proposal's
blocking + hybrid matching design:

  Standardize -> Block (birth year + 3-digit ZIP prefix) ->
  Deterministic tier (exact SSN, or exact name+DOB) ->
  Probabilistic tier (RapidFuzz name similarity, thresholded) ->
  Master Enterprise Patient ID (EPI) assignment via union-find
"""

import sqlite3
import uuid
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "db" / "vitalink.db"

AUTO_MATCH_THRESHOLD = 90
REVIEW_LOWER_THRESHOLD = 75


class UnionFind:
    def __init__(self, items):
        self.parent = {item: item for item in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def standardize_name(first, last):
    return f"{str(first).strip().lower()} {str(last).strip().lower()}"


def resolve_patients(patients_df):
    df = patients_df.copy()
    df["birth_year"] = df["dob"].astype(str).str[:4]
    df["zip_prefix"] = df["zip_code"].astype(str).str[:3]
    df["std_name"] = df.apply(lambda r: standardize_name(r["first_name"], r["last_name"]), axis=1)

    uf = UnionFind(df["local_id"].tolist())
    review_rows = []

    blocks = defaultdict(list)
    for _, row in df.iterrows():
        blocks[(row["birth_year"], row["zip_prefix"])].append(row)

    for block_rows in blocks.values():
        for row_a, row_b in combinations(block_rows, 2):
            same_ssn = row_a["ssn"] == row_b["ssn"]
            same_name_dob = row_a["std_name"] == row_b["std_name"] and row_a["dob"] == row_b["dob"]

            if same_ssn or same_name_dob:
                uf.union(row_a["local_id"], row_b["local_id"])
                continue

            if row_a["dob"] != row_b["dob"]:
                continue

            score = fuzz.ratio(row_a["std_name"], row_b["std_name"])
            if score >= AUTO_MATCH_THRESHOLD:
                uf.union(row_a["local_id"], row_b["local_id"])
            elif REVIEW_LOWER_THRESHOLD <= score < AUTO_MATCH_THRESHOLD:
                review_rows.append({
                    "local_id_a": row_a["local_id"],
                    "local_id_b": row_b["local_id"],
                    "name_a": row_a["std_name"],
                    "name_b": row_b["std_name"],
                    "dob": row_a["dob"],
                    "similarity_score": score,
                })

    root_to_epi = {}
    epi_map = {}
    for local_id in df["local_id"]:
        root = uf.find(local_id)
        if root not in root_to_epi:
            root_to_epi[root] = f"EPI_{uuid.uuid4().hex[:8].upper()}"
        epi_map[local_id] = root_to_epi[root]

    return epi_map, review_rows


def run_entity_resolution():
    print("Running Entity Resolution / Patient Matching...")
    conn = sqlite3.connect(DB_PATH)
    try:
        patients_df = pd.read_sql("SELECT * FROM stg_ehr_patients", conn)
        labs_df = pd.read_sql("SELECT * FROM stg_lab_results", conn)

        epi_map, review_rows = resolve_patients(patients_df)

        patients_df["enterprise_patient_id"] = patients_df["local_id"].map(epi_map)
        labs_df["enterprise_patient_id"] = labs_df["local_patient_id"].map(epi_map)

        patients_df.to_sql("dim_patients", conn, if_exists="replace", index=False)
        labs_df.to_sql("fact_lab_results", conn, if_exists="replace", index=False)

        review_df = pd.DataFrame(review_rows) if review_rows else pd.DataFrame(
            columns=["local_id_a", "local_id_b", "name_a", "name_b", "dob", "similarity_score"]
        )
        review_df.to_sql("entity_resolution_review_queue", conn, if_exists="replace", index=False)

        n_staged = patients_df["local_id"].nunique()
        n_master = patients_df["enterprise_patient_id"].nunique()

        print(f"  Staged patient records: {n_staged}")
        print(f"  Resolved master patient profiles: {n_master}")
        print(f"  Duplicate records merged: {n_staged - n_master}")
        print(f"  Pairs flagged for manual review (score {REVIEW_LOWER_THRESHOLD}-{AUTO_MATCH_THRESHOLD}): {len(review_rows)}")
    finally:
        conn.close()

    print("Entity Resolution complete!")


if __name__ == "__main__":
    run_entity_resolution()
