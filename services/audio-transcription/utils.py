import json
import os


def load_json_file(file_path):
    try:
        if not os.path.exists(file_path):
            return None
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"[Error] Invalid JSON in {file_path}: {e}")
        return None
    except Exception as e:
        print(f"[Error] Failed to read {file_path}: {e}")
        return None


def save_to_shared_disk(shared_path, job_id, filename, data):
    output_dir = os.path.join(shared_path, job_id)
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, filename), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
