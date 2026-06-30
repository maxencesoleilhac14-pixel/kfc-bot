#!/bin/bash
# Restore Telethon session from env var if provided (gzip+base64)
if [ -n "$SESSION_BASE64" ]; then
    python -c "
import base64, gzip, sys
data = base64.b64decode(sys.argv[1])
decompressed = gzip.decompress(data)
sys.stdout.buffer.write(decompressed)
" "$SESSION_BASE64" > session_analyze.session && echo "Session file restored from SESSION_BASE64" || echo "Failed to restore session"
fi

exec python bot.py
