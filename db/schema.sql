-- VitaLink Pipeline — db/schema.sql
--
-- Reference schema for db/vitalink.db. The pipeline creates these
-- tables automatically at runtime via pandas.to_sql() (see src/01_ingest.py
-- and src/02_entity_resolution.py) — this file documents that structure
-- for reference and to satisfy the "designed schema/model" deliverable.
-- Running this file manually is optional; it is NOT required to run the
-- pipeline, and re-running the pipeline will replace these tables anyway.

-- ============================================================
-- STAGING TABLES  (created by src/01_ingest.py)
-- Cleaned, validated records prior to entity resolution.
-- ============================================================

CREATE TABLE IF NOT EXISTS stg_ehr_patients (
    local_id    TEXT PRIMARY KEY,   -- source system's patient ID, e.g. 'EHR00001'
    first_name  TEXT NOT NULL,
    last_name   TEXT NOT NULL,
    dob         TEXT NOT NULL,      -- normalized to YYYY-MM-DD
    zip_code    TEXT NOT NULL,      -- 5-digit, zero-padded
    ssn         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stg_lab_results (
    lab_id            TEXT PRIMARY KEY,
    local_patient_id  TEXT NOT NULL,   -- FK -> stg_ehr_patients.local_id
    test_code         TEXT NOT NULL,   -- LOINC code
    result_value      REAL NOT NULL,
    result_date       TEXT NOT NULL    -- normalized to YYYY-MM-DD
);

-- ============================================================
-- RESOLVED TABLES  (created by src/02_entity_resolution.py)
-- Staging tables plus the resolved enterprise_patient_id assigned
-- by the entity resolution stage's blocking + matching logic.
-- ============================================================

CREATE TABLE IF NOT EXISTS dim_patients (
    local_id                TEXT PRIMARY KEY,
    first_name              TEXT NOT NULL,
    last_name               TEXT NOT NULL,
    dob                     TEXT NOT NULL,
    zip_code                TEXT NOT NULL,
    ssn                     TEXT NOT NULL,
    enterprise_patient_id   TEXT NOT NULL   -- 'EPI_xxxxxxxx'; shared across
                                              -- local_ids resolved as duplicates
);

CREATE TABLE IF NOT EXISTS fact_lab_results (
    lab_id                  TEXT PRIMARY KEY,
    local_patient_id        TEXT NOT NULL,
    test_code               TEXT NOT NULL,
    result_value            REAL NOT NULL,
    result_date             TEXT NOT NULL,
    enterprise_patient_id   TEXT   -- NULL indicates an orphaned lab record
                                    -- (local_patient_id not found in dim_patients);
                                    -- flagged by src/03_quality_checks.py
);

-- ============================================================
-- ENTITY RESOLUTION REVIEW QUEUE  (created by src/02_entity_resolution.py)
-- Candidate duplicate pairs scoring 75-89 on RapidFuzz name similarity
-- (>= 90 is auto-merged; < 75 is treated as distinct patients).
-- Corresponds to the proposal's "flagged for manual review" tier.
-- ============================================================

CREATE TABLE IF NOT EXISTS entity_resolution_review_queue (
    local_id_a         TEXT NOT NULL,
    local_id_b         TEXT NOT NULL,
    name_a             TEXT NOT NULL,
    name_b             TEXT NOT NULL,
    dob                TEXT NOT NULL,
    similarity_score   REAL NOT NULL
);

-- ============================================================
-- Sample queries demonstrating the use case
-- (patient-level lab activity; used in src/04_analytics.py)
-- ============================================================

-- Top patients by lab activity, resolved to a single master identity
-- even if they had multiple local_id entries in the source data:
--
-- SELECT p.enterprise_patient_id, p.first_name, p.last_name,
--        COUNT(l.lab_id) AS total_lab_tests,
--        MIN(l.result_date) AS first_lab_date,
--        MAX(l.result_date) AS last_lab_date
-- FROM dim_patients p
-- LEFT JOIN fact_lab_results l ON p.enterprise_patient_id = l.enterprise_patient_id
-- GROUP BY p.enterprise_patient_id
-- ORDER BY total_lab_tests DESC
-- LIMIT 10;

-- Duplicate patient records still awaiting manual review:
--
-- SELECT * FROM entity_resolution_review_queue
-- ORDER BY similarity_score DESC;
