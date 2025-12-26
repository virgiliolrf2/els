# Base Image with PyTorch & CUDA
FROM pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime

# Install System Dependencies
RUN apt-get update && apt-get install -y \
    git \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Install Python Dependencies for Elysium Worker
RUN pip install --no-cache-dir \
    hivemind \
    transformers \
    cryptography \
    pandas \
    scikit-learn \
    opencv-python-headless \
    numpy

# Set Working Directory
WORKDIR /app

# The runner script will be mounted or copied here by the Node manager
# COPY elysium_runner.py /app/elysium_runner.py (We will rely on volume mount for dev, or copy in prod.
# For this Dockerfile, we will COPY it to ensure it's baked in if built.)
COPY elysium_runner.py /app/elysium_runner.py
COPY elysium_crypto.py /app/elysium_crypto.py

# Entrypoint
ENTRYPOINT ["python", "elysium_runner.py"]
