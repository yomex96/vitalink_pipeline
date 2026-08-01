# run_pipeline.py
import subprocess

def main():
    print("🚀 Starting VitaLink Pipeline execution...\n")
    
    subprocess.run(["python3", "src/01_ingest.py"], check=True)
    subprocess.run(["python3", "src/02_entity_resolution.py"], check=True)
    subprocess.run(["python3", "src/03_quality_checks.py"], check=True)
    subprocess.run(["python3", "src/04_analytics.py"], check=True)
    
    print("\n🎉 VitaLink Pipeline completed successfully!")

if __name__ == "__main__":
    main()
