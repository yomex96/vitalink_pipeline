-- Operational Storage Tables
CREATE TABLE IF NOT EXISTS stg_ehr_patients (
    local_id VARCHAR(50),
    first_name VARCHAR(50),
    last_name VARCHAR(50),
    dob DATE,
    zip_code VARCHAR(10),
    ssn VARCHAR(11)
);

CREATE TABLE IF NOT EXISTS stg_lab_results (
    lab_id VARCHAR(50),
    local_patient_id VARCHAR(50),
    test_code VARCHAR(50), -- LOINC
    result_value VARCHAR(50),
    result_date DATE
);

CREATE TABLE IF NOT EXISTS master_patient_index (
    master_patient_id VARCHAR(36) PRIMARY KEY,
    source_system VARCHAR(50),
    source_local_id VARCHAR(50),
    match_confidence REAL,
    review_status VARCHAR(20) -- 'AUTOMATED', 'MANUAL_REVIEW', 'NEW_RECORD'
);
