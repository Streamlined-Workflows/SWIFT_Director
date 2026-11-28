#!/usr/bin/env python3
"""Song analysis service for the storyboard: lyrics with timestamps, tempo, beats, sections.

Runs beside SWIFT Director on the same machine (port 8191, local only). The storyboard asks it to analyze a
song by URL and polls for the result. Speech-to-text runs on the CPU with faster-whisper; the first
run downloads the speech model.

Optionally the voice is separated from the music first (Demucs, also on the CPU) and only the voice is transcribed.
Tempo, beats and sections always come from the full song. The separated voice is kept as a small MP3 so the page can
play it back; the newest few are kept and older ones are deleted.
"""
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("ASSIST_PORT", "8191"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
JOBS = {}                      # key -> {"state", "progress", "result", "error"}
RUN_LOCK = threading.Lock()    # one analysis at a time
_model = None
_separator = None
SEPARATOR = os.environ.get("SEPARATION_MODEL", "htdemucs")
VOCALS_DIR = os.environ.get("VOCALS_DIR", os.path.join(os.path.expanduser("~"), ".cache", "swift-vocals"))
VOCALS_KEPT = 12


def separation():
    """Can voices be separated here? -> (yes or no, why not)"""
    try:
        import demucs.apply      # noqa: F401
        import demucs.pretrained  # noqa: F401
        return True, ""
    except Exception as e:
        return False, f"The separation software is not installed in the song analysis service ({type(e).__name__})."


def vocals_path(key):
    return os.path.join(VOCALS_DIR, hashlib.sha256(key.encode()).hexdigest()[:32] + ".mp3")


def separate_vocals(src, tmp, key, job):
    """Split the voice from the music. -> path of a WAV holding only the voice. Also keeps an MP3 of it for playback."""
    global _separator
    import numpy as np
    import soundfile as sf
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model
    if _separator is None:
        job["progress"] = "Loading the separation model (the first run downloads about 80 MB)"
        _separator = get_model(SEPARATOR)
        _separator.eval()
    job["progress"] = "Separating the voice from the music (this adds a few minutes)"
    stereo = os.path.join(tmp, "stereo.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-vn", "-ac", "2", "-ar", str(_separator.samplerate), stereo], check=True, capture_output=True, timeout=300)
    data, rate = sf.read(stereo, dtype="float32", always_2d=True)
    wav = torch.from_numpy(np.ascontiguousarray(data.T))
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    with torch.no_grad():
        out = apply_model(_separator, ((wav - mean) / std)[None], device="cpu", split=True, overlap=0.25, progress=False)[0]
    voice = (out[list(_separator.sources).index("vocals")] * std + mean).clamp(-1, 1).numpy().T
    path = os.path.join(tmp, "vocals.wav")
    sf.write(path, voice, rate)
    try:                                              # a copy to listen to; failing to keep it does not fail the analysis
        os.makedirs(VOCALS_DIR, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-ac", "1", "-b:a", "128k", vocals_path(key)], check=True, capture_output=True, timeout=300)
        kept = sorted((os.path.join(VOCALS_DIR, f) for f in os.listdir(VOCALS_DIR) if f.endswith(".mp3")), key=os.path.getmtime, reverse=True)
        for old in kept[VOCALS_KEPT:]:
            os.remove(old)
    except (OSError, subprocess.SubprocessError):
        pass
    return path


def analyze_beats(wav_path):
    import librosa
    import numpy as np
    y, sr = librosa.load(wav_path, sr=22050, mono=True)
    duration = float(len(y) / sr)
    hop = 512
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=onset, sr=sr, hop_length=hop)
    tempo = float(np.atleast_1d(tempo)[0])
    beat_frames = np.asarray(beat_frames, dtype=int)
    beats = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop)

    # Assume 4/4 and pick the bar phase whose beats hit hardest.
    downbeats = beats
    if len(beat_frames) >= 8:
        strength = onset[np.clip(beat_frames, 0, len(onset) - 1)]
        phase = int(np.argmax([strength[p::4].mean() for p in range(4)]))
        downbeats = beats[phase::4]

    # Sections: cluster beat-synchronous timbre and harmony into contiguous blocks.
    sections = []
    if len(beat_frames) >= 16:
        mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13, hop_length=hop)
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop)
        feats = np.vstack([librosa.util.normalize(mfcc, axis=1), librosa.util.normalize(chroma, axis=1)])
        synced = librosa.util.sync(feats, beat_frames, aggregate=np.median)
        k = int(max(3, min(12, round(duration / 22.0))))
        k = min(k, synced.shape[1] - 1)
        bounds = librosa.segment.agglomerative(synced, k)
        edge_frames = np.concatenate([[0], beat_frames])          # column i of `synced` starts here
        times = sorted({0.0, *[float(librosa.frames_to_time(edge_frames[min(b, len(edge_frames) - 1)], sr=sr, hop_length=hop)) for b in bounds], duration})
        rms = librosa.feature.rms(y=y, hop_length=hop)[0]
        rms_t = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)
        peak = float(rms.max()) or 1.0
        for a, b in zip(times[:-1], times[1:]):
            if b - a < 1.0:
                continue
            mask = (rms_t >= a) & (rms_t < b)
            sections.append({"start": round(a, 2), "end": round(b, 2),
                             "energy": round(float(rms[mask].mean() / peak) if mask.any() else 0.0, 3)})
    return {"duration": round(duration, 3), "tempo": round(tempo, 1),
            "beats": [round(float(t), 3) for t in beats], "downbeats": [round(float(t), 3) for t in downbeats],
            "sections": sections}


def transcribe(audio_path, job):
    global _model
    from faster_whisper import WhisperModel
    if _model is None:
        job["progress"] = "Loading the speech model (the first run downloads about 1.6 GB)"
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 8)
    job["progress"] = "Transcribing the lyrics"
    # Hand the speech model the samples directly (16 kHz mono). Letting it open the file itself breaks on
    # newer versions of its audio library ("unexpected keyword argument 'metadata_errors'").
    import librosa
    samples, _ = librosa.load(audio_path, sr=16000, mono=True)
    segments, info = _model.transcribe(samples.astype("float32"), word_timestamps=True, beam_size=5,
                                       condition_on_previous_text=False, vad_filter=False)
    lines = []
    for seg in segments:
        text = (seg.text or "").strip()
        if not text or seg.no_speech_prob > 0.85 or seg.avg_logprob < -1.3:
            continue                                   # likely noise or an instrumental passage
        lines.append({"start": round(float(seg.start), 2), "end": round(float(seg.end), 2), "text": text})
        job["progress"] = f"Transcribing the lyrics ({int(seg.end // 60)}:{int(seg.end % 60):02d} so far)"
    return {"language": info.language, "lines": lines}


def run_job(key, url, separate=False):
    job = JOBS[key]
    with RUN_LOCK:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                job["progress"] = "Fetching the song"
                src = os.path.join(tmp, "song")
                with urllib.request.urlopen(url, timeout=120) as r, open(src, "wb") as f:
                    f.write(r.read())
                wav = os.path.join(tmp, "song.wav")
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "22050", wav],
                               check=True, capture_output=True, timeout=300)
                job["progress"] = "Finding the tempo, beats and sections"
                result = analyze_beats(wav)
                if separate:
                    result.update(transcribe(separate_vocals(src, tmp, key, job), job))
                else:
                    try:
                        os.remove(vocals_path(key))          # an earlier separated voice no longer matches this analysis
                    except OSError:
                        pass
                    result.update(transcribe(wav, job))
                result["separated"] = bool(separate)
            job.update(state="done", progress="Done", result=result)
        except subprocess.CalledProcessError as e:
            job.update(state="error", error="Could not read that audio file: " + (e.stderr or b"").decode("utf-8", "replace")[-300:])
        except Exception as e:                           # report anything else to the page
            traceback.print_exc()
            job.update(state="error", error=f"{type(e).__name__}: {e}"[:500])


MODEL_UPDATE = {"state": "idle", "error": ""}


def speech_repo():
    from faster_whisper.utils import _MODELS
    return _MODELS.get(WHISPER_MODEL, WHISPER_MODEL)


def model_info():
    """Which version of the speech model is on disk, and which one is published now."""
    try:
        from huggingface_hub import HfApi, constants
        repo = speech_repo()
        local = None
        try:
            with open(os.path.join(constants.HF_HUB_CACHE, "models--" + repo.replace("/", "--"), "refs", "main")) as f:
                local = f.read().strip()
        except OSError:
            pass
        return {"repo": repo, "local": local, "remote": HfApi().model_info(repo).sha,
                "update": MODEL_UPDATE["state"], "update_error": MODEL_UPDATE["error"]}
    except Exception as e:
        return {"error": f"Could not check the speech model: {type(e).__name__}: {e}"[:300]}


def update_model():
    global _model
    try:
        from faster_whisper.utils import download_model
        download_model(WHISPER_MODEL)
        with RUN_LOCK:               # not while a song is being transcribed
            _model = None            # the next analysis loads the new files
        MODEL_UPDATE.update(state="idle", error="")
    except Exception as e:
        MODEL_UPDATE.update(state="error", error=f"{type(e).__name__}: {e}"[:300])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/status":
            key = urllib.parse.parse_qs(url.query).get("key", [""])[0]
            return self.send_json(JOBS.get(key) or {"state": "none"})
        if url.path == "/model":
            return self.send_json(model_info())
        if url.path == "/health":
            can, why = separation()
            return self.send_json({"ok": True, "model": WHISPER_MODEL, "separation": can, "separation_reason": why, "separation_model": SEPARATOR})
        if url.path == "/vocals":
            key = urllib.parse.parse_qs(url.query).get("key", [""])[0]
            try:
                with open(vocals_path(key), "rb") as f:
                    body = f.read()
            except OSError:
                return self.send_json({"error": "The separated voice for this song is not kept here any more."}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        if self.path == "/model/update":
            if MODEL_UPDATE["state"] != "running":
                MODEL_UPDATE.update(state="running", error="")
                threading.Thread(target=update_model, daemon=True).start()
            return self.send_json(MODEL_UPDATE)
        if self.path != "/analyze":
            return self.send_json({"error": "Not found"}, 404)
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        key, url, separate = payload.get("key"), payload.get("url"), payload.get("separate") is True
        if not key or not url:
            return self.send_json({"error": "key and url are required"}, 400)
        if separate and not separation()[0]:
            return self.send_json({"error": separation()[1]}, 409)
        if JOBS.get(key, {}).get("state") != "running":
            JOBS[key] = {"state": "running", "progress": "Waiting to start"}
            threading.Thread(target=run_job, args=(key, url, separate), daemon=True).start()
        self.send_json(JOBS[key])


if __name__ == "__main__":
    print(f"Assist service on 127.0.0.1:{PORT} (speech model: {WHISPER_MODEL})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
