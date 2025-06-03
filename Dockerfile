# 1. Use Python 3.13 slim as the base image
FROM python:3.13-slim

# 2. Install OS‐level dependencies required for Ollama
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       curl \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 3. Install the Ollama CLI via its official installer
RUN curl -sSfL https://ollama.com/install.sh | sh

# 4. Temporarily start Ollama, pull the llama3 model, then stop Ollama
#    (same trick as before, but without --daemon)
RUN ollama serve & \
    pid=$$! && \
    sleep 10 && \
    ollama pull llama3 && \
    kill $pid

# 5. Create and switch to /app directory
WORKDIR /app

# 6. Copy requirements.txt and install Python dependencies
COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# 7. Copy the rest of your application code (main.py, JSON files, etc.)
COPY . .

# 8. Copy the entrypoint script into the image and make sure it’s executable
#    (entrypoint.sh is now in /app)
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# 9. At container startup, run entrypoint.sh (which starts Ollama, waits, then runs your bot)
ENTRYPOINT ["/app/entrypoint.sh"]
