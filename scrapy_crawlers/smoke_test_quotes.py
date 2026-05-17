import subprocess
import os
import sys

def run_smoke_test():
    print("Starting smoke test for quotes_spider...")
    
    # Ensure PYTHONPATH is set so scrapy can find the project modules
    env = os.environ.copy()
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    env["PYTHONPATH"] = f"{project_root}:{env.get('PYTHONPATH', '')}"
    
    # Cleanup previous runs
    for i in [1, 2]:
        filename = f"quotes-{i}.html"
        if os.path.exists(filename):
            os.remove(filename)
            print(f"Removed old {filename}")

    # Run the spider
    print("Running 'scrapy crawl quotes'...")
    try:
        result = subprocess.run(
            ["scrapy", "crawl", "quotes"],
            env=env,
            capture_output=True,
            text=True,
            check=True
        )
        print("Scrapy execution completed successfully.")
    except subprocess.CalledProcessError as e:
        print(f"Error running scrapy: {e}")
        print(f"Stdout: {e.stdout}")
        print(f"Stderr: {e.stderr}")
        return False

    # Verify output files
    success = True
    for i in [1, 2]:
        filename = f"quotes-{i}.html"
        if os.path.exists(filename):
            size = os.path.getsize(filename)
            if size > 0:
                print(f"SUCCESS: {filename} created and has size {size} bytes.")
            else:
                print(f"FAILURE: {filename} is empty.")
                success = False
        else:
            print(f"FAILURE: {filename} was not created.")
            success = False
            
    if success:
        print("\nSmoke test PASSED!")
    else:
        print("\nSmoke test FAILED!")
        
    return success

if __name__ == "__main__":
    if run_smoke_test():
        sys.exit(0)
    else:
        sys.exit(1)
