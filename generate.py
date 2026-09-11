import json
import os
import time
import requests
from dotenv import load_dotenv

import kie_vision

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
KIE_API_KEY = os.getenv("KIE_API_KEY", "")

MAX_RETRIES = 4

UPLOAD_URL = "https://kieai.redpandaai.co/api/file-stream-upload"
CREATE_TASK_URL = "https://api.kie.ai/api/v1/jobs/createTask"
GET_TASK_URL = "https://api.kie.ai/api/v1/jobs/recordInfo"

HEADERS_AUTH = {"Authorization": f"Bearer {KIE_API_KEY}"}

PROMPT_FILE = os.path.join(os.path.dirname(__file__), "prompt.txt")


def load_prompt(prompt_file: str = None) -> str:
    path = prompt_file or PROMPT_FILE
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def upload_image(file_path: str) -> str:
    """Upload a local image and return its public URL.

    Delegates to kie_vision.upload_image — same KIE file-stream-upload
    endpoint, one shared implementation (retry + shared account-wide rate
    limiting) for both the analysis and AI-generation paths."""
    return kie_vision.upload_image(file_path, upload_path="screenshots", mime="image/png")


def create_task(image_url: str, prompt_file: str = None, prompt: str = None) -> str:
    payload = {
        "model": "nano-banana-2-lite",
        "input": {
            "prompt": prompt if prompt is not None else load_prompt(prompt_file),
            "image_input": [image_url],
            "aspect_ratio": "auto"
        },
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                CREATE_TASK_URL,
                headers={**HEADERS_AUTH, "Content-Type": "application/json"},
                json=payload,
            )
            if response.status_code == 429:
                # KIE rejects (does not queue) requests over the account's
                # 20-per-10s cap — back off a full window, not the generic 3s.
                print(f"  create_task rate-limited (429), attempt {attempt}/{MAX_RETRIES} — waiting 11s")
                if attempt == MAX_RETRIES:
                    response.raise_for_status()
                time.sleep(11)
                continue
            response.raise_for_status()
            data = response.json()
            if data.get("code") != 200:
                raise RuntimeError(f"Task creation failed: {data}")
            return data["data"]["taskId"]
        except Exception as e:
            print(f"  create_task attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt == MAX_RETRIES:
                raise
            time.sleep(3 * attempt)


def poll_task(task_id: str, timeout: int = 800) -> str:
    """Poll until task completes and return the result image URL."""
    start = time.time()
    interval = 15
    while time.time() - start < timeout:
        response = requests.get(
            GET_TASK_URL,
            headers=HEADERS_AUTH,
            params={"taskId": task_id},
        )
        response.raise_for_status()
        resp = response.json()
        app_code = resp.get("code")
        if app_code not in (200, None):
            raise RuntimeError(f"Poll error (code {app_code}): {resp.get('msg')}")
        data = resp.get("data", {})
        state = data.get("state")

        if state == "success":
            result = json.loads(data["resultJson"])
            return result["resultUrls"][0]
        elif state == "fail":
            raise RuntimeError(f"Task failed [{data.get('failCode', '')}]: {data.get('failMsg')}")

        print(f"  [{task_id}] state={state}, waiting {interval}s...")
        time.sleep(interval)
        interval = min(interval + 5, 30)

    raise TimeoutError(f"Task {task_id} timed out after {timeout}s")


def download_image(url: str, output_path: str):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, timeout=60)
            response.raise_for_status()
            with open(output_path, "wb") as f:
                f.write(response.content)
            return
        except Exception as e:
            print(f"  download_image attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt == MAX_RETRIES:
                raise
            time.sleep(3 * attempt)

