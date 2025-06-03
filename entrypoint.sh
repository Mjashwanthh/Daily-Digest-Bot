#!/bin/sh

# 1. Start the Ollama server in the background
ollama serve &

# 2. Give it a few seconds to be ready
sleep 5

# 3. Now run the Slack‐Bolt bot
python main.py

