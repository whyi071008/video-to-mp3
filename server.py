import os
import re
import shutil
import json
import struct
import subprocess
import tempfile
import uuid
import threading
from pathlib import Path
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# --- Configuration ---
BASE_DIR = Path(__file__).parent
WORK_DIR = BASE_DIR / "work"
OUTPUT_DIR = BASE_DIR / "outputs"
WORK_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024  # 300 MB

# --- In-memory job state ---
jobs = {}

TRIMMING_RULES = {
    "douyin": {
        "label": "抖音 (剪掉结尾 2.5秒)",
        "trim_end": 2.5,
    },
    "tangdou": {
        "label": "糖豆广场舞 (剪掉开头 3秒)",
        "trim_start": 3.0,
    },
    "kuaishou": {
        "label": "快手 (剪掉结尾 2秒)",
        "trim_end": 2.0,
    },
}

# --- Helpers ---

def sanitize_filename(name: str) -> str:
    """Replace invalid filename characters with underscores."""
    return re.sub(r'[\\/:*?"<>|]', '_', name)

def detect_platform(text: str) -> str | None:
    lower = text.lower()
    if "douyin" in lower or "抖音" in text or "tiktok" in lower:
        return "douyin"
    if "tangdou" in lower or "糖豆" in text:
        return "tangdou"
    if "kuaishou" in lower or "快手" in text:
        return "kuaishou"
    return None

def get_audio_duration(video_path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", video_path],
        capture_output=True, text=True, timeout=30
    )
    return float(result.stdout.strip())

def generate_waveform_peaks(video_path: str, num_peaks: int = 800) -> list:
    tmp_wav = os.path.join(tempfile.mkdtemp(dir=WORK_DIR), "waveform.wav")
    tmpdir = os.path.dirname(tmp_wav)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-ac", "1", "-ar", "8000",
             "-acodec", "pcm_s16le", "-t", "600", tmp_wav],
            capture_output=True, text=True, timeout=120
        )
        if not os.path.exists(tmp_wav):
            return []

        with open(tmp_wav, "rb") as f:
            f.seek(44)
            raw = f.read()

        samples = struct.unpack(f"<{len(raw) // 2}h", raw)
        window = max(1, len(samples) // num_peaks)
        peaks = []
        for i in range(0, len(samples), window):
            chunk = samples[i:i + window]
            if not chunk:
                break
            rms = (sum(s * s for s in chunk) / len(chunk)) ** 0.5
            peaks.append(min(1.0, rms / 32768.0))

        if len(peaks) > num_peaks:
            step = len(peaks) / num_peaks
            peaks = [peaks[int(i * step)] for i in range(num_peaks)]

        return peaks
    except Exception:
        return []
    finally:
        try:
            shutil.rmtree(tmpdir)
        except OSError:
            pass

def process_video(job_id, source, source_type, trim_start=0, trim_end=0, display_name=""):
    jobs[job_id] = {"status": "processing", "progress": 0, "file_path": None, "error": None}
    tmpdir = None

    try:
        tmpdir = tempfile.mkdtemp(dir=WORK_DIR)

        if source_type == "url":
            jobs[job_id]["progress"] = 10
            output_template = os.path.join(tmpdir, "%(title)s.%(ext)s")
            result = subprocess.run(
                ["python3", "-m", "yt_dlp", "-f", "bestaudio/best", "-o", output_template,
                 "--no-playlist", "--max-filesize", "500m", source],
                capture_output=True, text=True, timeout=300, cwd=tmpdir
            )
            if result.returncode != 0:
                err = result.stderr.strip()[-300:] if result.stderr else "未知错误"
                raise Exception(f"下载失败: {err}")

            files = [f for f in os.listdir(tmpdir) if not f.startswith('.')]
            if not files:
                raise Exception("下载完成但未找到文件")
            video_path = os.path.join(tmpdir, files[0])
            safe_name = re.sub(r'[^\w\-.]', '_', Path(files[0]).stem)
        else:
            jobs[job_id]["progress"] = 20
            video_path = source
            safe_name = re.sub(r'[^\w\-.]', '_', Path(source).stem)

        jobs[job_id]["progress"] = 40

        duration = get_audio_duration(video_path)
        jobs[job_id]["duration"] = duration

        mp3_path = os.path.join(tmpdir, f"{safe_name}.mp3")
        ffmpeg_cmd = ["ffmpeg", "-y", "-i", video_path]

        if trim_start > 0 or trim_end > 0:
            start_sec = max(0, trim_start)
            end_sec = min(duration, duration - trim_end if trim_end > 0 else duration)
            if end_sec > start_sec:
                ffmpeg_cmd.extend(["-af", f"atrim={start_sec}:{end_sec}"])

        ffmpeg_cmd.extend(["-q:a", "2", mp3_path])

        jobs[job_id]["progress"] = 60

        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            err = result.stderr.strip()[-300:] if result.stderr else "未知错误"
            raise Exception(f"转换失败: {err}")

        jobs[job_id]["progress"] = 90

        # Use display name if provided, sanitize and fallback to safe_name
        final_stem = sanitize_filename(display_name) if display_name else safe_name
        if not final_stem:
            final_stem = safe_name
        final_name = final_stem + ".mp3"
        final_path = OUTPUT_DIR / final_name

        counter = 1
        while final_path.exists():
            final_name = f"{final_stem}_{counter}.mp3"
            final_path = OUTPUT_DIR / final_name
            counter += 1

        os.rename(mp3_path, str(final_path))

        jobs[job_id]["status"] = "done"
        jobs[job_id]["progress"] = 100
        jobs[job_id]["file_path"] = str(final_path)
        jobs[job_id]["file_name"] = final_name

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
    finally:
        if tmpdir and os.path.isdir(tmpdir):
            try:
                shutil.rmtree(tmpdir)
            except OSError:
                pass


# --- Routes ---

@app.route("/")
def home():
    return jsonify({"status": "running"})

@app.route("/api/platforms")
def api_platforms():
    return jsonify(TRIMMING_RULES)

@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"error": "未选择文件"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "未选择文件"}), 400

    ext = Path(file.filename).suffix.lower()
    allowed = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v", ".3gp"}
    if ext not in allowed:
        return jsonify({"error": f"不支持的格式: {ext}"}), 400

    saved_path = os.path.join(WORK_DIR, f"upload_{uuid.uuid4().hex[:8]}{ext}")
    file.save(saved_path)

    try:
        duration = get_audio_duration(saved_path)
        peaks = generate_waveform_peaks(saved_path)
    except Exception:
        duration = 0
        peaks = []

    return jsonify({
        "path": saved_path,
        "filename": file.filename,
        "duration": duration,
        "waveform": peaks,
    })

@app.route("/api/waveform", methods=["POST"])
def api_waveform():
    data = request.get_json(force=True)
    path = data.get("path", "")
    if not path or not os.path.exists(path):
        return jsonify({"error": "文件不存在"}), 400
    try:
        duration = get_audio_duration(path)
        peaks = generate_waveform_peaks(path)
        return jsonify({"duration": duration, "waveform": peaks})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/process", methods=["POST"])
def api_process():
    data = request.get_json(force=True)
    source = data.get("source", "").strip()
    source_type = data.get("source_type", "url")
    platform = data.get("platform", "none")
    trim_start = float(data.get("trim_start", 0))
    trim_end = float(data.get("trim_end", 0))
    display_name = data.get("display_name", "").strip()
    auto_detect_text = data.get("auto_detect_text", "").strip()

    if not source:
        return jsonify({"error": "请提供视频链接或上传文件"}), 400

    if platform == "none" and auto_detect_text:
        detected = detect_platform(auto_detect_text)
        if detected and detected in TRIMMING_RULES:
            platform = detected
            rule = TRIMMING_RULES[detected]
            if "trim_start" in rule:
                trim_start = rule["trim_start"]
            if "trim_end" in rule:
                trim_end = rule["trim_end"]
    elif platform in TRIMMING_RULES:
        rule = TRIMMING_RULES[platform]
        if "trim_start" in rule:
            trim_start = rule["trim_start"]
        if "trim_end" in rule:
            trim_end = rule["trim_end"]

    job_id = str(uuid.uuid4())[:8]

    thread = threading.Thread(
        target=process_video,
        args=(job_id, source, source_type, trim_start, trim_end, display_name)
    )
    thread.daemon = True
    thread.start()

    return jsonify({
        "job_id": job_id,
        "platform": platform,
        "trim_start": trim_start,
        "trim_end": trim_end,
    })

@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(job)

@app.route("/api/download/<job_id>")
def api_download(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "文件未就绪"}), 404
    return send_file(
        job["file_path"],
        as_attachment=True,
        download_name=job.get("file_name", "audio.mp3")
    )

@app.route("/api/media/<path:filepath>")
def api_media(filepath):
    """Serve uploaded video files for audio preview. Protected against path traversal."""
    # Resolve the real path and verify it stays within WORK_DIR
    requested = os.path.realpath(os.path.join(str(WORK_DIR), filepath))
    work_real = os.path.realpath(str(WORK_DIR))
    if not requested.startswith(work_real + os.sep) and requested != work_real:
        return jsonify({"error": "禁止访问"}), 403
    if not os.path.isfile(requested):
        return jsonify({"error": "文件不存在"}), 404
    return send_file(requested)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5100))
    print(f"Server starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
