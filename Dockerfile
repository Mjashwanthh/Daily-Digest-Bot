# 1. Use Python 3.13 slim as the base image (matches your local Python 3.13.3)
FROM python:3.13-slim

# 2. Install OS‐level dependencies required for the Ollama installer
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       curl \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 3. Install the Ollama CLI via its official installer script
RUN curl -sSfL https://ollama.com/install.sh | sh

# 4. Start the Ollama server in "daemon" mode, wait for it, pull llama3, then stop it
#    This ensures that "ollama pull llama3" can connect to a running Ollama server.
RUN ollama serve --daemon \
    && sleep 10 \
    && ollama pull llama3 \
    && ollama stop

# 5. Create and switch to /app directory for our code
WORKDIR /app

# 6. Copy requirements.txt and install Python dependencies
COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# 7. Copy the rest of the application code (including main.py, JSON files, etc.)
COPY . .

# 8. When the container starts, run our bot in Socket Mode
CMD ["python", "main.py"]
