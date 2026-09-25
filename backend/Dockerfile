# Persona onboarding backend: FastAPI + Claude brain + Pipecat voice.
FROM python:3.13-slim

# Runtime libraries for the audio stack (onnxruntime for VAD/Smart Turn, aiortc/av for WebRTC).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 libglib2.0-0 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# Sessions + logs. Mount a volume here so sessions survive redeploys.
ENV DB_PATH=/data/sessions.db
RUN mkdir -p /data
VOLUME ["/data"]

ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
