# 1. Use Python 3.13 slim as the base image
FROM python:3.13-slim

# 2. Install OS dependencies required by Ollama
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       curl \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 3. Install Ollama CLI via the official installer
RUN curl -sSfL https://ollama.com/install.sh | sh

# 4. Pull the llama3 model so it's baked into the image
RUN ollama pull llama3

# 5. Create working directory
WORKDIR /app

# 6. Copy and install Python dependencies
COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# 7. Copy all remaining application files
COPY . .

# 8. Run your bot
CMD ["python", "main.py"]

