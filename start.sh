#!/bin/bash
# Restore Telethon session from env var if provided
if [ -n "$SESSION_BASE64" ]; then
    echo "$SESSION_BASE64" | base64 -d > session_analyze.session
    echo "Session file restored from SESSION_BASE64"
fi

python bot.py
