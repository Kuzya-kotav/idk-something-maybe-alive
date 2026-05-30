Remote Auto Chess Pi - run notes

Goal:
Raspberry Pi 5 + Camera Rev 1.3 sees a fixed chess board from above.
Pieces have colored markers by type: P/N/B/R/Q/K.
The app infers the human white move, asks local Stockfish for black move, then sends MOVE commands to Arduino Mega via USB serial.
The web UI is hosted on the Pi over local Wi-Fi/LAN.
No webhooks. No cloud API.

Files:
remote_chess_pi.py     one main code file
install_pi.sh          installs Pi dependencies
remote_chess_config.json is created automatically after first run

Install on Raspberry Pi:
cd remote_chess_pi_project
chmod +x install_pi.sh
./install_pi.sh

Run:
python3 remote_chess_pi.py

Open from Windows browser:
http://RASPBERRY_PI_IP:5000

Find Pi IP:
hostname -I

Arduino expected serial protocol:
PING
ZERO
POS
MOVE E2 E4
STOP

Serial defaults:
/dev/ttyACM0 or /dev/ttyUSB0
115200 baud

Stockfish:
The installer uses the local apt stockfish package.
The Python app talks to Stockfish by UCI subprocess. It is local, not online.

First setup steps in the browser:
1. Check live camera stream.
2. Adjust board rectangle in remote_chess_config.json if the grid is not exactly on the board.
3. Teach colors: choose P/N/B/R/Q/K, then click that colored piece in the camera stream.
4. Put pieces in the normal start position.
5. Human moves white physically.
6. Press Scan human move.
7. Press Engine move.

Important limitation:
The app recognizes piece TYPE by color, not white/black from color.
Side is tracked by the internal chess game state:
Human = white.
PC/Stockfish = black.
This is correct for your planned project.

Critical physical-board limitation:
Captures are hard mechanically. If a target square is occupied, Arduino usually needs a capture/removal routine before MOVE.
Default config sends only MOVE FROM TO.
If your Arduino supports CAPTURE E4, set capture_remove_enabled=true in remote_chess_config.json.

Board rectangle config:
remote_chess_config.json:
"board": { "rect": [x, y, width, height], "white_bottom": true }

If red/blue are inverted:
remote_chess_config.json:
"camera": { "color_order": "RGB" }
try RGB or BGR, then restart the app.

Stop:
Ctrl+C
