# Song analysis for the storyboard: faster-whisper (lyrics) and librosa (beats), CPU only.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libsndfile1 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir faster-whisper librosa soundfile
# Optional: separating the voice from the music (Demucs, CPU). Allowed to fail so the service still builds without it;
# the page then shows the option as unavailable. No model file is in the image: it is fetched on first use.
RUN (pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu && pip install --no-cache-dir demucs) \
    || echo "Voice separation could not be installed. Everything else works."
WORKDIR /srv
COPY service.py .
CMD ["python", "service.py"]
