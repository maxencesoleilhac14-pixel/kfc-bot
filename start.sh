#!/bin/bash
# Restore Telethon session from env var if provided (gzip+base64)
if [ -n "$SESSION_BASE64" ]; then
    echo "$SESSION_BASE64" | base64 -d | gunzip -c > session_analyze.session 2>/dev/null || echo "$SESSION_BASE64" | base64 -d > session_analyze.session
    echo "Session file restored from SESSION_BASE64"
fi

python bot.py
