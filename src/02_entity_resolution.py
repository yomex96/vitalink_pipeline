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


def match_pharmacy_to_master(pharmacy_df, dim_patients_df):
    """
    Genuine cross-namespace entity resolution: pharmacy records carry
    NO shared identifier with the EHR/master patient index — only a
    name and DOB. Each pharmacy record must be matched against the
    already-resolved master patient list using the same blocking +
    deterministic/probabilistic design, or else minted as a new
    patient known only through the pharmacy source (per the proposal's
    bias-mitigation commitment to include patients from any single
    source rather than only those matched across all sources).

    Returns:
        rx_epi_map: dict[pharmacy_record_id -> enterprise_patient_id]
        review_rows: list of dicts for ambiguous (75-89) matches
    """
    rx_df = pharmacy_df.copy()
    rx_df["std_name"] = rx_df.apply(
        lambda r: standardize_name(r["patient_first_name"], r["patient_last_name"]), axis=1
    )
    rx_df["birth_year"] = rx_df["patient_dob"].astype(str).str[:4]

    master_df = dim_patients_df.copy()
    master_df["std_name"] = master_df.apply(
        lambda r: standardize_name(r["first_name"], r["last_name"]), axis=1
    )
    master_df["birth_year"] = master_df["dob"].astype(str).str[:4]

    # Block master patients by birth year for fast lookup (no ZIP
    # available on the pharmacy side, so blocking uses birth year only).
    master_by_year = defaultdict(list)
    for _, row in master_df.iterrows():
        master_by_year[row["birth_year"]].append(row)

    rx_epi_map = {}
    review_rows = []

    for _, rx_row in rx_df.iterrows():
        candidates = master_by_year.get(rx_row["birth_year"], [])
        best_epi = None
        best_score = 0

        for cand in candidates:
            # Deterministic tier: exact standardized name + DOB.
            if cand["std_name"] == rx_row["std_name"] and cand["dob"] == rx_row["patient_dob"]:
                best_epi = cand["enterprise_patient_id"]
                best_score = 100
                break

            # Probabilistic tier: only score candidates with an exact DOB match.
            if cand["dob"] != rx_row["patient_dob"]:
                continue
            score = fuzz.ratio(rx_row["std_name"], cand["std_name"])
            if score > best_score:
                best_score = score
                best_candidate_epi = cand["enterprise_patient_id"]
                best_candidate_name = cand["std_name"]

        if best_epi:
            rx_epi_map[rx_row["pharmacy_record_id"]] = best_epi
        elif best_score >= AUTO_MATCH_THRESHOLD:
            rx_epi_map[rx_row["pharmacy_record_id"]] = best_candidate_epi
        elif REVIEW_LOWER_THRESHOLD <= best_score < AUTO_MATCH_THRESHOLD:
            review_rows.append({
                "pharmacy_record_id": rx_row["pharmacy_record_id"],
                "pharmacy_name": rx_row["std_name"],
                "candidate_master_name": best_candidate_name,
                "candidate_enterprise_patient_id": best_candidate_epi,
                "dob": rx_row["patient_dob"],
                "similarity_score": best_score,
            })
            rx_epi_map[rx_row["pharmacy_record_id"]] = None  # pending review
        else:
            # No match found in the master index at all: this patient
            # is known ONLY through the pharmacy source. Mint a new
            # enterprise_patient_id rather than dropping the record —
            # per the proposal, excluding single-source patients would
            # bias the pipeline toward already-engaged patients.
            rx_epi_map[rx_row["pharmacy_record_id"]] = f"EPI_{uuid.uuid4().hex[:8].upper()}"

    return rx_epi_map, review_rows


def run_entity_resolution():
    print("Running Entity Resolution / Patient Matching...")
    conn = sqlite3.connect(DB_PATH)
    try:
        patients_df = pd.read_sql("SELECT * FROM stg_ehr_patients", conn)
        labs_df = pd.read_sql("SELECT * FROM stg_lab_results", conn)
        pharmacy_df = pd.read_sql("SELECT * FROM stg_pharmacy_logs", conn)

        epi_map, review_rows = resolve_patients(patients_df)

        patients_df["enterprise_patient_id"] = patients_df["local_id"].map(epi_map)
        labs_df["enterprise_patient_id"] = labs_df["local_patient_id"].map(epi_map)

        patients_df.to_sql("dim_patients", conn, if_exists="replace", index=False)
        labs_df.to_sql("fact_lab_results", conn, if_exists="replace", index=False)

        review_df = pd.DataFrame(review_rows) if review_rows else pd.DataFrame(
            columns=["local_id_a", "local_id_b", "name_a", "name_b", "dob", "similarity_score"]
        )
        review_df.to_sql("entity_resolution_review_queue", conn, if_exists="replace", index=False)

        # Cross-source resolution: match pharmacy records (no shared ID)
        # against the now-resolved master patient index.
        rx_epi_map, rx_review_rows = match_pharmacy_to_master(pharmacy_df, patients_df)
        pharmacy_df["enterprise_patient_id"] = pharmacy_df["pharmacy_record_id"].map(rx_epi_map)
        pharmacy_df.to_sql("fact_pharmacy_fulfillment", conn, if_exists="replace", index=False)

        rx_review_df = pd.DataFrame(rx_review_rows) if rx_review_rows else pd.DataFrame(
            columns=["pharmacy_record_id", "pharmacy_name", "candidate_master_name",
                     "candidate_enterprise_patient_id", "dob", "similarity_score"]
        )
        rx_review_df.to_sql("pharmacy_entity_resolution_review_queue", conn, if_exists="replace", index=False)

        n_staged = patients_df["local_id"].nunique()
        n_master = patients_df["enterprise_patient_id"].nunique()
        n_rx = len(pharmacy_df)
        n_rx_matched_existing = pharmacy_df["enterprise_patient_id"].isin(patients_df["enterprise_patient_id"]).sum()
        n_rx_new_patients = n_rx - n_rx_matched_existing - len(rx_review_rows)

        print(f"  Staged patient records: {n_staged}")
        print(f"  Resolved master patient profiles: {n_master}")
        print(f"  Duplicate records merged (within EHR source): {n_staged - n_master}")
        print(f"  Pairs flagged for manual review (score {REVIEW_LOWER_THRESHOLD}-{AUTO_MATCH_THRESHOLD}): {len(review_rows)}")
        print(f"  Pharmacy records: {n_rx} total, {n_rx_matched_existing} matched to existing "
              f"EHR patients (cross-source), {n_rx_new_patients} identified only via pharmacy, "
              f"{len(rx_review_rows)} flagged for manual review")
    finally:
        conn.close()

    print("Entity Resolution complete!")


if __name__ == "__main__":
    run_entity_resolution()
