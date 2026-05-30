#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
echo "Installing Remote Auto Chess Pi dependencies..."
sudo apt update
sudo apt install -y \
  python3-picamera2 \
  python3-opencv \
  python3-flask \
  python3-serial \
  python3-chess \
  stockfish \
  v4l-utils
python3 - <<'PY'
import cv2, flask, chess
from picamera2 import Picamera2
print('Python imports OK')
PY
chmod +x remote_chess_pi.py
echo "Done. Run: python3 remote_chess_pi.py"
