# Debian-based Python, because PyTorch's CUDA wheels need glibc
FROM python:3.11-slim-bookworm

LABEL author="p1zzm1ssile"
LABEL org.opencontainers.image.source="https://github.com/p1zzm1ssile/Narrator"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.description="Narrator: turns stories into audiobooks with a cast of expressive voices (Chatterbox), an LLM that works out who says what and how they feel, Kokoro as a fast fallback, and a Whisper check that re-renders garbled lines."

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg joins the clips and makes the MP3 or M4B
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# PyTorch 2.6 from PyPI is built for CUDA 12.4: GTX 10-series up to RTX 40-series
# (an RTX 2080 Super is fine). The CUDA libraries come inside the wheels, so the
# host only needs the NVIDIA driver (Unraid's Nvidia Driver plugin).
# RTX 50-series cards need a newer PyTorch than Chatterbox 0.1.7 allows, so aren't supported yet.
ARG TORCH_VERSION=2.6.0
ARG TORCH_INDEX=
RUN pip install torch==${TORCH_VERSION} torchaudio==${TORCH_VERSION} ${TORCH_INDEX:+--index-url $TORCH_INDEX}

WORKDIR /app
COPY src/requirements.txt .
RUN pip install -r requirements.txt

COPY src .

# Settings, voices, stories, finished audiobooks and downloaded models live in /config.
# Map it to appdata. The models are about 4 GB and are downloaded on first use.
ENV CONFIG_DIR=/config \
    HF_HOME=/config/models/huggingface \
    WEB_PORT=9895 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib:/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib
RUN mkdir -p /config
VOLUME /config
EXPOSE 9895

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD ["python3", "healthcheck.py"]
CMD ["python3", "main.py"]
