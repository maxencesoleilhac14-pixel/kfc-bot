#!/bin/bash
# Restore Telethon session from env var if provided (gzip+base64)
rm -f session_analyze.session session_analyze.session-journal
if [ -n "$SESSION_BASE64" ]; then
    echo "$SESSION_BASE64" | python -c "
import base64, gzip, sys
data = base64.b64decode(sys.stdin.read().strip())
decompressed = gzip.decompress(data)
sys.stdout.buffer.write(decompressed)
" > session_analyze.session && echo "Session file restored from SESSION_BASE64" || echo "Failed to restore session"
fi

exec python bot.py
