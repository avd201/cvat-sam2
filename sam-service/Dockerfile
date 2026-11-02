# Build context is the repository ROOT (see docker-compose.sam2.yml) so that sam2common/ is available.
# CUDA 12.1 runtime image; torch>=2.5.1 is required by sam2 (setup.py).
FROM pytorch/pytorch:2.5.1-cuda12.1-cudnn9-runtime
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
# Pinned to the sam2 commit inspected during research (HEAD of main, 2024-12-15).
ARG SAM2_COMMIT=2b90b9f5ceec907a1c18123530e92e794ad901a4
RUN pip install --no-cache-dir "git+https://github.com/facebookresearch/sam2.git@${SAM2_COMMIT}"
WORKDIR /srv
COPY sam-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sam2common ./sam2common
COPY sam-service/app ./app
# SEG_BACKEND: "sam2" = image + video (Stage 2), "sam2-image" = image only (Stage 1 behaviour).
ENV SEG_BACKEND=sam2 \
    SAM2_CONFIG=configs/sam2.1/sam2.1_hiera_l.yaml \
    SAM2_CHECKPOINT=/checkpoints/sam2.1_hiera_large.pt \
    DEVICE=cuda
EXPOSE 8000
# ONE worker on purpose: model and caches live in this single process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
