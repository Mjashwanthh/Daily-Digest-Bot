# 1. Use Python 3.13 slim as the base
FROM python:3.13-slim

# 2. Install OS‐level dependencies needed by Ollama
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       curl \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 3. Install the Ollama CLI via its official installer
RUN curl -sSfL https://ollama.com/install.sh | sh

# 4. Start Ollama in the background, wait, pull the llama3 model, then stop Ollama
#    - 'ollama serve &' runs Ollama server in background
#    - 'pid=$!' captures its PID
#    - 'sleep 10' lets the server finish starting
#    - 'ollama pull llama3' downloads llama3
#    - 'kill $pid' stops Ollama before proceeding
RUN ollama serve & \
    pid=$! && \
    sleep 10 && \
    ollama pull llama3 && \
    kill $pid

# 5. Switch to /app directory for our application
WORKDIR /app

# 6. Copy requirements and install Python dependencies
COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# 7. Copy the rest of the code (main.py, JSON files, entrypoint.sh, etc.)
COPY . .

# 8. Ensure entrypoint.sh is executable
RUN chmod +x /app/entrypoint.sh

# 9. Use entrypoint.sh as the container’s startup script
ENTRYPOINT ["/app/entrypoint.sh"]
