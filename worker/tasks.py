import os
import time
import shutil
import requests
import xmltodict
import json
from celery import Celery

# --- 1. 灵活配置区 (支持 Docker/Apptainer 环境变量) ---
redis_host = os.getenv('REDIS_HOST', '127.0.0.1')
redis_port = os.getenv('REDIS_PORT', '6379')

# 获取外部 Service 地址
audio_host = os.getenv('AUDIO_HOST', "localhost")
audio_port = os.getenv('AUDIO_PORT', '9001')
visual_host = os.getenv('VISUAL_HOST', "localhost")
visual_port = os.getenv('VISUAL_PORT', '9002')
visual_api_admin_key = os.getenv('ADMIN_KEY', 'ai4me_admin_password')

# 拼接 API 完整路径 (注意：根据你之前的 FastAPI 代码，路径是 /process_audio/)
audio_api_url = f"http://{audio_host}:{audio_port}/process_audio/"
visual_api_url = f"http://{visual_host}:{visual_port}" # 这里假设视觉服务的 FastAPI 监听根路径，实际使用时请根据你的代码调整

shared_path = os.getenv("SHARED_PATH", "/app/tmp")
api_key_path = os.getenv("API_KEY_PATH", "/app/data") # 视觉服务 API key 存储路径

# --- 2. Celery 实例初始化 ---
app = Celery('tasks', 
             broker=f'redis://{redis_host}:{redis_port}/0', 
             backend=f'redis://{redis_host}:{redis_port}/0')

app.conf.update(
    broker_transport_options={'visibility_timeout': 3600},  # 1小时超时
    result_expires=86400,            # 24小时过期
    worker_prefetch_multiplier=1,    # 公平调度：一次只领一个任务
    task_acks_late=True,             # 任务成功后再确认，防止崩溃丢任务
    result_persistent=True,          # 结果持久化
    task_reject_on_worker_lost=True,     # 工作丢失时重试任务
)

# --- 3. 辅助函数：提取有效路径 ---
def get_video_payload_path(full_path):
    """
    将 /app/tmp/uuid/video.mp4 转换为 uuid/video.mp4 
    以匹配 FastAPI 后端的 SHARED_PATH 拼接逻辑
    """
    path_parts = full_path.strip("/").split("/")
    if len(path_parts) < 2:
        return full_path
    return "/".join(path_parts[-2:])

# --- 4. 任务定义 ---

def save_to_disk(job_id, filename, data):
    output_dir = os.path.join(shared_path, job_id)
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, filename), 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def ensure_api_key(api_dir="/app/data", admin_key=visual_api_admin_key): # change the api_dir
    key_file_path = os.path.join(api_dir, "api.key")

    if os.path.exists(key_file_path):
        with open(key_file_path, 'r') as f:
            existing_key = f.read().strip()
            if existing_key:
                print(f"[Key Manager] API key already exists: {existing_key}")
                return existing_key
    else:
        print(f"[Key Manager] API key file not found. Creating new key at {key_file_path}")
        gen_url = visual_api_url + "/generate"
        headers = {
            "X-Admin-Key": admin_key,
            "Content-Type": "application/json"
        }
        payload = {
            "client_name": "client_ai4me",
            "expire_in_days": 365
        }

        try: 
            response = requests.post(gen_url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            new_key = data.get("api_key")

            if not new_key:
                raise ValueError(f"Failed to obtain API key from visual service: {data}")
            with open(key_file_path, 'w') as f:
                f.write(new_key)
            print(f"[Key Manager] Generated and saved new API key: {new_key}")

            os.chmod(key_file_path, 0o644)
            return new_key
    
        except Exception as e:
            print(f"[Key Manager] Error ensuring API key: {str(e)}")
            return None
        
def extract_flat_captions(xml_body):
    """
       [{"start": 0.0, "end": 15.0, "captions": "..."}, ...]
    """
    data = xmltodict.parse(xml_body)
    
    try:
        segments_node = data.get("VideoAnalysis", {}).get("Segments", {})
        raw_segments = segments_node.get("Segment", [])
    except (AttributeError, KeyError):
        return []

    if isinstance(raw_segments, dict):
        raw_segments = [raw_segments]
    
    return [
        {
            "start": float(seg.get("StartTime", 0)),
            "end": float(seg.get("EndTime", 0)),
            "captions": seg.get("Description", "")
        }
        for seg in raw_segments
    ]

@app.task(name="tasks.process_visual")
def process_visual(file_path):
    # file_path: /app/tmp/{task_id}/{filename}
    result_template = {"type": "visual", "success": False, "video_name": None,"output": None, "error": None}
    file_path = os.path.normpath(file_path)
    path_parts = file_path.split(os.sep)
    job_id = path_parts[-2]
    file_name = path_parts[-1]

    api_key = ensure_api_key()
    if not api_key:
        result_template["error"] = "Failed to obtain API key for visual service"
        result_template["video_name"] = file_name
        return result_template
    try:
        print(f"[Visual Worker] Starting Task: {file_path}")

        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        headers = {"X-API-Key": api_key}
        analyze_url = visual_api_url + "/analyze"        
        # real API calling
        with open(file_path, 'rb') as f:
            files = {'video': f}

            response = requests.post(
                analyze_url,
                headers=headers,
                files=files,
                timeout=6000
            )
        
        response.raise_for_status()
        xml_data = response.text

        outputs = extract_flat_captions(xml_data)

        result_template.update({"success": True, "video_name": file_name, "output": outputs})

        file_name_no_ext = os.path.splitext(file_name)[0]
        save_to_disk(job_id, f"{file_name_no_ext}_visual_output.json", result_template["output"])

    except Exception as e:
        print(f"[Visual Worker] Error: {str(e)}")
        result_template["error"] = str(e)
    return result_template


@app.task(name="tasks.process_audio")
def process_audio(file_path):
    result_template = {"type": "audio", "success": False, "video_name": None, "output": None, "error": None}
    file_path = os.path.normpath(file_path)
    path_parts = file_path.split(os.sep)
    job_id = path_parts[-2]
    file_name = path_parts[-1]

    try:
        print(f"[Audio Worker] Starting Task: {file_path}")
        
        if not os.path.exists(file_path):
            result_template["error"] = f"Physical file check failed: {file_path}"
            return result_template

        # 关键点：转换路径格式为 {task_id}/{filename}
        video_path_payload = get_video_payload_path(file_path)
        payload = {"video_path": video_path_payload}

        response = requests.post(audio_api_url, json=payload, timeout=1800)
        
        if response.status_code != 200:
            try:
                err_detail = response.json().get("detail", response.text)
            except:
                err_detail = response.text
            raise Exception(f"Audio Service Error ({response.status_code}): {err_detail}")

        service_data = response.json()

        outputs = []
        for entry in service_data.get("output", []):
            item = {
                "start": entry["start"].split(",")[0],
                "end": entry["end"].split(",")[0],
                 "caption": entry["caption"]
            }
            outputs.append(item)

        result_template.update({
            "success": True,
            "output": outputs,
            "video_name": file_name
        })
        print(f"[Audio Worker] Success: Received {len(result_template['output'])} items.")

        file_name_no_ext = os.path.splitext(file_name)[0]
        save_to_disk(job_id, f"{file_name_no_ext}_audio_output.json", outputs)

    except Exception as e:
        print(f"[Audio Worker] Error: {str(e)}")
        result_template["error"] = str(e)
    
    return result_template


@app.task(name="tasks.finalize_results")
def finalize_results(raw_results, job_id, callback_url=None):
    
    audio_data = {}
    visual_data = {}
    video_name = "unknown_video"

    for res in raw_results:
        if not isinstance(res, dict):
            continue
        res_type = res.get("type")
        if res_type == "audio":
            audio_data = res
            video_name = res.get("video_name", video_name)
        elif res_type == "visual":
            visual_data = res
            video_name = res.get("video_name", video_name)

    workspace = os.path.join(shared_path, job_id)

    all_success = all(res.get("success", False) for res in raw_results)

    video_name_no_ext = os.path.splitext(video_name)[0]
    with open(os.path.join(workspace, f"{video_name_no_ext}_task_info.txt"), 'w') as f:
        f.write(f"Job ID: {job_id}\n")
        f.write(f"Video Name: {video_name}\n")
        f.write(f"Status: {'Success' if all_success else 'Partial/Complete Failure'}\n")

    video_full_path = os.path.join(workspace, video_name)
    if all_success:
        if os.path.exists(video_full_path):
            try:
                os.remove(video_full_path)
                print(f"[Cleanup] Deleted source video: {video_name}")
            except Exception as e:
                print(f"[Cleanup Warning] Failed to delete {video_name}: {e}")
    else:
        print(f"[Finalize] Not all tasks succeeded. Preserving source video for debugging: {video_name}")

    final_output = {
        "job_id": job_id,
        "status": "success" if all_success else "partial_failure",
        "video_name": video_name,
        "audio_result": audio_data.get("output"),
        "visual_result": visual_data.get("output"),
    }

    if callback_url:
        try: 
            requests.post(callback_url, json=final_output, timeout=10)
        except Exception as e:
            print(f"[Callback Warning] Failed to send results to callback URL: {e}")

    return final_output