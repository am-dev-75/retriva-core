# Use Python 3.12 slim as base image
FROM python:3.12-slim

# Prevent Python from writing pyc files and keep stdout/stderr unbuffered
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# Set PYTHONPATH so the app can resolve the retriva package
ENV PYTHONPATH=/app/src

# Set working directory
WORKDIR /app

# Install system dependencies
# - tesseract-ocr & language packs: required by OCRmyPDF for scanning
# - ghostscript: required by OCRmyPDF
# - build-essential (gcc/g++/make): required by PyTorch Inductor to JIT-compile
#   CPU kernels for Docling's deep-learning layout/table models. Without a C++
#   compiler, torch._inductor raises "InvalidCxxCompiler: No working C++
#   compiler found" and Docling silently degrades to a lossy parse.
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-ita \
    ghostscript \
    curl \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Give the non-root user a writable TorchInductor kernel cache dir (created on
# first layout-model inference) and point Inductor at it.
ENV TORCHINDUCTOR_CACHE_DIR=/app/.torchinductor

# Create a non-root user and group
RUN useradd -m -U appuser && chown -R appuser:appuser /app

# Copy only requirements to cache them in docker layer
COPY requirements.txt /app/

# Install Python dependencies
# Using --no-cache-dir to reduce image size
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the source code
COPY --chown=appuser:appuser src /app/src

# Switch to non-root user
# Make rapidocr models dir writable so Docling can download model variants at runtime
RUN mkdir -p /app/storage && chown -R appuser:appuser /app/storage && \
    chmod -R a+rw /usr/local/lib/python3.12/site-packages/rapidocr/models/
USER appuser

# Expose ports (8000 for Ingestion API, 8001 for OpenAI API)
EXPOSE 8000 8001

# Add Healthcheck (supports both OpenAI API on 8001 and Ingestion API on 8000)
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8001/health || curl -f http://localhost:8000/health || exit 1

# The default command runs the OpenAI API (Core). It can be overridden in compose for Ingestion API.
CMD ["python", "-m", "retriva.openai_api", "--host", "0.0.0.0", "--port", "8001"]
