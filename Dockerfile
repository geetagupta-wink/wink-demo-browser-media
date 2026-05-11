FROM python:3.11-slim

# System libraries needed at runtime by mediapipe and opencv-python-headless.
# libgl1 + libglib2.0-0 cover the dynamic linker dependencies; we don't need
# the full opengl/X stack because we use opencv-python-headless. Slim base
# image keeps the final image around ~600MB after wheels are installed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps in their own layer so dep changes don't bust the
# (much heavier) source-code layer's cache.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the runtime app surface — index.html and server.py. The huge
# .y4m/.mp4 fakecam test media plus .env / .git are excluded by
# .dockerignore (and would otherwise blow up the build context).
COPY server.py index.html ./
# Sample videos served at /samples/ for the Fakecam Setup launcher to fetch.
COPY samples ./samples
# Setup + launch scripts served at /launcher/ as downloads for testers.
COPY launchers ./launchers

# Fly injects PORT=8080 by default; server.py reads it. Binding to 0.0.0.0
# is handled in server.py via the "PORT env var present → cloud" rule.
EXPOSE 8080

CMD ["python", "server.py"]
