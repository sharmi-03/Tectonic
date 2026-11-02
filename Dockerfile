# ===== Stage 1: Build Frontend =====
FROM node:20-alpine AS frontend-builder

WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ===== Stage 2: Python Backend with Full Agent Builder =====
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies including git for some packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for layer caching
COPY requirements.txt ./

# Install Python dependencies - full requirements for agent building
# Build version: 8 – fix API response shapes to match frontend TypeScript types
RUN pip install --no-cache-dir -r requirements.txt && \
    echo "=== Installed versions ===" && \
    pip show litellm | grep Version && \
    pip show anthropic | grep Version && \
    pip show mcp | grep Version

# Copy the full application code
COPY agent_builder/ ./agent_builder/
COPY server_full.py ./server.py

# NOTE: tectonic_runtime excluded - has syntax errors and heavy dependencies
# The server has graceful fallback when it's not available

# Copy built frontend from Stage 1
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

# Create directories and ensure writable
RUN mkdir -p /app/generated_agents /app/logs && \
    chmod -R 777 /app/generated_agents /app/logs

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV AGENT_BUILDER_HOST=0.0.0.0
ENV AGENT_BUILDER_PORT=8080
ENV AGENT_BUILDER_OUTPUT_DIR=/app/generated_agents
ENV AGENT_BUILDER_LOG_DIR=/app/logs
# Suppress ADK warnings about LiteLLM
ENV ADK_SUPPRESS_GEMINI_LITELLM_WARNINGS=true

EXPOSE 8080

CMD ["python", "server.py"]
