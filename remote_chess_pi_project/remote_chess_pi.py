#!/usr/bin/env python3
"""
Remote Auto Chess - Raspberry Pi main program

One-file project:
- Picamera2 board camera
- color-marker chess piece recognition
- legal move inference with python-chess
- local Stockfish engine using UCI
- Arduino Mega USB serial command output
- Flask web UI over local Wi-Fi/LAN

Default Arduino protocol:
PING
ZERO
POS
MOVE E2 E4
STOP

Run:
python3 remote_chess_pi.py
Open from PC: http://RASPBERRY_PI_IP:5000
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

MISSING = []
try:
    import cv2
except Exception:
    MISSING.append("python3-opencv")
try:
    import numpy as np
except Exception:
    MISSING.append("python3-numpy")
try:
    from flask import Flask, Response, jsonify, request
except Exception:
    MISSING.append("python3-flask")
try:
    from picamera2 import Picamera2
except Exception:
    MISSING.append("python3-picamera2")
try:
    import chess
except Exception:
    MISSING.append("python3-chess")
try:
    import serial
except Exception:
    serial = None

if MISSING:
    print("Missing packages:", ", ".join(MISSING))
    print("Run: sudo apt install -y " + " ".join(sorted(set(MISSING + ["python3-serial", "stockfish"]))))
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "remote_chess_config.json"
LOG_LIMIT = 300

DEFAULT_CONFIG: Dict[str, Any] = {
    "server": {"host": "0.0.0.0", "port": 5000},
    "camera": {
        "width": 960,
        "height": 720,
        "format": "RGB888",
        "color_order": "BGR",  # User's Pi Camera Rev 1.3 stream looked correct when treated as BGR.
        "jpeg_quality": 78,
        "fps_delay_s": 0.08
    },
    "board": {
        "rect": [120, 60, 720, 600],
        "white_bottom": True,
        "sample_margin_ratio": 0.25,
        "draw_grid": True
    },
    "vision": {
        "min_piece_pixels": 80,
        "dominance_ratio": 1.18,
        "learn_patch_radius": 10,
        "h_tolerance": 10,
        "s_tolerance": 65,
        "v_tolerance": 65
    },
    "piece_colors_hsv": {
        "P": [{"lower": [95, 60, 40], "upper": [135, 255, 255]}],
        "N": [{"lower": [40, 55, 40], "upper": [85, 255, 255]}],
        "B": [{"lower": [20, 60, 60], "upper": [38, 255, 255]}],
        "R": [
            {"lower": [0, 60, 40], "upper": [10, 255, 255]},
            {"lower": [170, 60, 40], "upper": [179, 255, 255]}
        ],
        "Q": [{"lower": [130, 40, 40], "upper": [165, 255, 255]}],
        "K": [{"lower": [8, 60, 60], "upper": [22, 255, 255]}]
    },
    "piece_names": {
        "P": "pawn",
        "N": "knight",
        "B": "bishop",
        "R": "rook",
        "Q": "queen",
        "K": "king"
    },
    "draw_colors_bgr": {
        "P": [255, 0, 0],
        "N": [0, 255, 0],
        "B": [0, 255, 255],
        "R": [0, 0, 255],
        "Q": [255, 0, 255],
        "K": [0, 140, 255]
    },
    "serial": {
        "enabled": True,
        "baud": 115200,
        "port_candidates": ["/dev/ttyACM0", "/dev/ttyUSB0", "/dev/ttyAMA0"],
        "move_command_format": "MOVE {from_sq} {to_sq}",
        "capture_remove_enabled": False,
        "capture_command_format": "CAPTURE {to_sq}",
        "read_after_write_s": 0.15,
        "zero_on_start": False
    },
    "stockfish": {
        "path": "stockfish",
        "skill_level": 6,
        "movetime_ms": 700,
        "depth": 8,
        "use_depth": False
    },
    "game": {
        "human_color": "white",
        "engine_color": "black",
        "auto_engine_after_human": False,
        "scan_clear_score": 6,
        "scan_margin": 2,
        "allow_manual_confirm": True
    }
}


def deep_merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = json.loads(json.dumps(a))
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                return deep_merge(DEFAULT_CONFIG, json.load(f))
        except Exception as e:
            print(f"Config load failed, using defaults: {e}")
    save_config(DEFAULT_CONFIG)
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg: Dict[str, Any]) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


config = load_config()
app = Flask(__name__)


@dataclass
class SharedState:
    lock: threading.RLock = field(default_factory=threading.RLock)
    raw_frame_bgr: Optional[Any] = None
    annotated_jpg: Optional[bytes] = None
    raw_jpg: Optional[bytes] = None
    observed_types: Dict[str, str] = field(default_factory=dict)
    detections: List[Dict[str, Any]] = field(default_factory=list)
    board: Any = field(default_factory=lambda: chess.Board())
    move_log: List[Dict[str, Any]] = field(default_factory=list)
    arduino_log: List[str] = field(default_factory=list)
    app_log: List[str] = field(default_factory=list)
    candidate_moves: List[Dict[str, Any]] = field(default_factory=list)
    last_error: str = ""
    running: bool = True
    serial_status: str = "not connected"
    stockfish_status: str = "not tested"


state = SharedState()


def add_log(msg: str) -> None:
    line = time.strftime("%H:%M:%S") + " | " + str(msg)
    with state.lock:
        state.app_log.append(line)
        state.app_log[:] = state.app_log[-LOG_LIMIT:]
    print(line, flush=True)


def add_arduino_log(msg: str) -> None:
    line = time.strftime("%H:%M:%S") + " | " + str(msg)
    with state.lock:
        state.arduino_log.append(line)
        state.arduino_log[:] = state.arduino_log[-LOG_LIMIT:]
    print("ARDUINO", line, flush=True)


class ArduinoLink:
    def __init__(self):
        self.ser = None
        self.lock = threading.Lock()

    def connect(self) -> bool:
        if not config["serial"].get("enabled", True):
            with state.lock:
                state.serial_status = "disabled in config"
            return False
        if serial is None:
            with state.lock:
                state.serial_status = "python serial missing"
            return False
        if self.ser and getattr(self.ser, "is_open", False):
            return True
        baud = int(config["serial"].get("baud", 115200))
        for port in config["serial"].get("port_candidates", []):
            try:
                self.ser = serial.Serial(port, baudrate=baud, timeout=0.2, write_timeout=0.5)
                time.sleep(2.0)
                with state.lock:
                    state.serial_status = f"connected {port} @ {baud}"
                add_arduino_log(f"connected {port} @ {baud}")
                if config["serial"].get("zero_on_start", False):
                    self.send("ZERO")
                return True
            except Exception as e:
                with state.lock:
                    state.serial_status = f"failed {port}: {e}"
        add_arduino_log("no Arduino serial port found; commands will be logged only")
        return False

    def send(self, line: str) -> Dict[str, Any]:
        line = line.strip()
        if not line:
            return {"ok": False, "error": "empty command"}
        with self.lock:
            connected = self.connect()
            add_arduino_log("> " + line)
            if not connected:
                return {"ok": False, "sent": line, "error": "serial not connected; logged only"}
            try:
                self.ser.write((line + "\n").encode("utf-8"))
                self.ser.flush()
                time.sleep(float(config["serial"].get("read_after_write_s", 0.15)))
                replies = []
                while self.ser.in_waiting:
                    replies.append(self.ser.readline().decode("utf-8", errors="replace").strip())
                for r in replies:
                    if r:
                        add_arduino_log("< " + r)
                return {"ok": True, "sent": line, "reply": replies}
            except Exception as e:
                with state.lock:
                    state.serial_status = f"write error: {e}"
                add_arduino_log("ERROR " + str(e))
                return {"ok": False, "sent": line, "error": str(e)}


arduino = ArduinoLink()


def square_name_from_row_col(row: int, col: int) -> str:
    white_bottom = bool(config["board"].get("white_bottom", True))
    if white_bottom:
        file_i = col
        rank_i = 7 - row
    else:
        file_i = 7 - col
        rank_i = row
    return chess.square_name(chess.square(file_i, rank_i))


def row_col_from_square(square_name: str) -> Tuple[int, int]:
    sq = chess.parse_square(square_name)
    file_i = chess.square_file(sq)
    rank_i = chess.square_rank(sq)
    if config["board"].get("white_bottom", True):
        return 7 - rank_i, file_i
    return rank_i, 7 - file_i


def hue_ranges_from_sample(h: int, s: int, v: int) -> List[Dict[str, List[int]]]:
    ht = int(config["vision"].get("h_tolerance", 10))
    st = int(config["vision"].get("s_tolerance", 65))
    vt = int(config["vision"].get("v_tolerance", 65))
    s1, s2 = max(0, s - st), min(255, s + st)
    v1, v2 = max(0, v - vt), min(255, v + vt)
    low_h = h - ht
    high_h = h + ht
    if low_h < 0:
        return [
            {"lower": [0, s1, v1], "upper": [high_h, s2, v2]},
            {"lower": [180 + low_h, s1, v1], "upper": [179, s2, v2]},
        ]
    if high_h > 179:
        return [
            {"lower": [low_h, s1, v1], "upper": [179, s2, v2]},
            {"lower": [0, s1, v1], "upper": [high_h - 180, s2, v2]},
        ]
    return [{"lower": [low_h, s1, v1], "upper": [high_h, s2, v2]}]


def detect_board(frame_bgr: Any) -> Tuple[Any, Dict[str, str], List[Dict[str, Any]]]:
    cfg_board = config["board"]
    cfg_vis = config["vision"]
    x, y, bw, bh = [int(v) for v in cfg_board["rect"]]
    margin_ratio = float(cfg_board.get("sample_margin_ratio", 0.25))
    cell_w = bw / 8.0
    cell_h = bh / 8.0
    min_pixels = int(cfg_vis.get("min_piece_pixels", 80))
    dominance = float(cfg_vis.get("dominance_ratio", 1.18))

    annotated = frame_bgr.copy()
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    observed: Dict[str, str] = {}
    dets: List[Dict[str, Any]] = []

    if cfg_board.get("draw_grid", True):
        cv2.rectangle(annotated, (x, y), (x + bw, y + bh), (255, 255, 255), 2)
        for i in range(1, 8):
            xx = int(x + i * cell_w)
            yy = int(y + i * cell_h)
            cv2.line(annotated, (xx, y), (xx, y + bh), (120, 120, 120), 1)
            cv2.line(annotated, (x, yy), (x + bw, yy), (120, 120, 120), 1)

    for row in range(8):
        for col in range(8):
            sx1 = int(x + col * cell_w + cell_w * margin_ratio)
            sy1 = int(y + row * cell_h + cell_h * margin_ratio)
            sx2 = int(x + (col + 1) * cell_w - cell_w * margin_ratio)
            sy2 = int(y + (row + 1) * cell_h - cell_h * margin_ratio)
            sx1, sy1 = max(0, sx1), max(0, sy1)
            sx2, sy2 = min(frame_bgr.shape[1] - 1, sx2), min(frame_bgr.shape[0] - 1, sy2)
            if sx2 <= sx1 or sy2 <= sy1:
                continue

            square = square_name_from_row_col(row, col)
            roi_hsv = hsv[sy1:sy2, sx1:sx2]
            scores = []
            for ptype, ranges in config["piece_colors_hsv"].items():
                mask_total = np.zeros(roi_hsv.shape[:2], dtype=np.uint8)
                for r in ranges:
                    lo = np.array(r["lower"], dtype=np.uint8)
                    hi = np.array(r["upper"], dtype=np.uint8)
                    mask_total = cv2.bitwise_or(mask_total, cv2.inRange(roi_hsv, lo, hi))
                score = int(cv2.countNonZero(mask_total))
                scores.append((score, ptype))
            scores.sort(reverse=True)
            best_score, best_type = scores[0]
            second_score = scores[1][0] if len(scores) > 1 else 0
            if best_score >= min_pixels and best_score >= max(1, int(second_score * dominance)):
                observed[square] = best_type
                bgr = tuple(int(v) for v in config["draw_colors_bgr"].get(best_type, [0, 255, 0]))
                cx1 = int(x + col * cell_w)
                cy1 = int(y + row * cell_h)
                cx2 = int(x + (col + 1) * cell_w)
                cy2 = int(y + (row + 1) * cell_h)
                cv2.rectangle(annotated, (cx1 + 2, cy1 + 2), (cx2 - 2, cy2 - 2), bgr, 3)
                label = f"{square}:{best_type}"
                cv2.rectangle(annotated, (cx1 + 2, cy1 + 2), (cx1 + 90, cy1 + 24), bgr, -1)
                cv2.putText(annotated, label, (cx1 + 5, cy1 + 19), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 2)
                dets.append({"square": square, "piece_type": best_type, "pixels": best_score, "box": [cx1, cy1, cx2, cy2]})
            else:
                # draw square label lightly for calibration
                cx1 = int(x + col * cell_w)
                cy1 = int(y + row * cell_h)
                cv2.putText(annotated, square, (cx1 + 4, cy1 + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (180, 180, 180), 1)

    return annotated, observed, dets


def camera_loop() -> None:
    add_log("camera starting")
    picam2 = None
    while state.running:
        try:
            if picam2 is None:
                picam2 = Picamera2()
                cam_cfg = config["camera"]
                c = picam2.create_video_configuration(main={
                    "size": (int(cam_cfg["width"]), int(cam_cfg["height"])),
                    "format": cam_cfg.get("format", "RGB888")
                })
                picam2.configure(c)
                picam2.start()
                time.sleep(1.0)
                add_log("camera ready")
            arr = picam2.capture_array()
            if config["camera"].get("color_order", "BGR").upper() == "RGB":
                frame_bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            else:
                frame_bgr = arr
            annotated, observed, dets = detect_board(frame_bgr)
            quality = int(config["camera"].get("jpeg_quality", 78))
            ok_raw, raw_buf = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
            ok_ann, ann_buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if ok_raw and ok_ann:
                with state.lock:
                    state.raw_frame_bgr = frame_bgr
                    state.raw_jpg = raw_buf.tobytes()
                    state.annotated_jpg = ann_buf.tobytes()
                    state.observed_types = observed
                    state.detections = dets
                    state.last_error = ""
            time.sleep(float(config["camera"].get("fps_delay_s", 0.08)))
        except Exception as e:
            with state.lock:
                state.last_error = str(e)
            add_log("camera error: " + str(e))
            time.sleep(1.0)


def board_type_map(board_obj: Any) -> Dict[str, str]:
    out = {}
    for sq, piece in board_obj.piece_map().items():
        out[chess.square_name(sq)] = piece.symbol().upper()
    return out


def board_piece_map_text(board_obj: Any) -> Dict[str, str]:
    out = {}
    for sq, piece in board_obj.piece_map().items():
        out[chess.square_name(sq)] = piece.symbol()
    return out


def compare_type_maps(expected: Dict[str, str], observed: Dict[str, str]) -> Tuple[int, List[str]]:
    score = 0
    problems = []
    for sq in chess.SQUARE_NAMES:
        e = expected.get(sq)
        o = observed.get(sq)
        if e == o:
            continue
        if e is None and o is not None:
            score += 2
            problems.append(f"extra {o} on {sq}")
        elif e is not None and o is None:
            score += 2
            problems.append(f"missing {e} on {sq}")
        else:
            score += 3
            problems.append(f"wrong {sq}: expected {e}, saw {o}")
    return score, problems[:10]


def legal_move_candidates_from_observation() -> List[Dict[str, Any]]:
    with state.lock:
        current_board = state.board.copy()
        observed = dict(state.observed_types)

    candidates = []
    for mv in current_board.legal_moves:
        test_board = current_board.copy()
        test_board.push(mv)
        expected = board_type_map(test_board)
        score, problems = compare_type_maps(expected, observed)
        candidates.append({
            "uci": mv.uci(),
            "san": current_board.san(mv),
            "from": chess.square_name(mv.from_square),
            "to": chess.square_name(mv.to_square),
            "score": score,
            "problems": problems
        })
    candidates.sort(key=lambda x: x["score"])
    return candidates[:8]


def apply_move_uci(uci: str, source: str, send_to_arduino: bool = False) -> Dict[str, Any]:
    with state.lock:
        board_obj = state.board
        try:
            mv = chess.Move.from_uci(uci)
        except Exception as e:
            return {"ok": False, "error": f"bad uci: {e}"}
        if mv not in board_obj.legal_moves:
            return {"ok": False, "error": f"illegal move {uci} in current board"}
        san = board_obj.san(mv)
        is_capture = board_obj.is_capture(mv)
        from_sq = chess.square_name(mv.from_square)
        to_sq = chess.square_name(mv.to_square)
        board_obj.push(mv)
        entry = {
            "ply": len(state.move_log) + 1,
            "source": source,
            "uci": uci,
            "san": san,
            "from": from_sq,
            "to": to_sq,
            "capture": bool(is_capture),
            "fen": board_obj.fen(),
            "time": time.strftime("%H:%M:%S")
        }
        state.move_log.append(entry)
        state.candidate_moves = []
    add_log(f"move {source}: {san} ({uci})")

    serial_result = None
    if send_to_arduino:
        if is_capture and config["serial"].get("capture_remove_enabled", False):
            cap_cmd = config["serial"].get("capture_command_format", "CAPTURE {to_sq}").format(to_sq=to_sq.upper())
            arduino.send(cap_cmd)
        cmd = config["serial"].get("move_command_format", "MOVE {from_sq} {to_sq}").format(
            from_sq=from_sq.upper(), to_sq=to_sq.upper(), uci=uci
        )
        serial_result = arduino.send(cmd)
    return {"ok": True, "move": entry, "serial": serial_result}


def stockfish_best_move(fen: str) -> Dict[str, Any]:
    sf_cfg = config["stockfish"]
    path = sf_cfg.get("path", "stockfish")
    try:
        p = subprocess.Popen(
            [path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
    except Exception as e:
        with state.lock:
            state.stockfish_status = f"start failed: {e}"
        return {"ok": False, "error": f"Cannot start Stockfish at '{path}': {e}"}

    def send(cmd: str) -> None:
        assert p.stdin is not None
        p.stdin.write(cmd + "\n")
        p.stdin.flush()

    def read_until(token: str, timeout: float = 5.0) -> List[str]:
        assert p.stdout is not None
        lines = []
        end = time.time() + timeout
        while time.time() < end:
            line = p.stdout.readline()
            if not line:
                break
            line = line.strip()
            lines.append(line)
            if token in line:
                break
        return lines

    try:
        send("uci")
        read_until("uciok", 5.0)
        skill = int(sf_cfg.get("skill_level", 6))
        send(f"setoption name Skill Level value {skill}")
        send("isready")
        read_until("readyok", 5.0)
        send(f"position fen {fen}")
        if sf_cfg.get("use_depth", False):
            send(f"go depth {int(sf_cfg.get('depth', 8))}")
        else:
            send(f"go movetime {int(sf_cfg.get('movetime_ms', 700))}")
        lines = read_until("bestmove", 20.0)
        best_line = next((l for l in reversed(lines) if l.startswith("bestmove")), "")
        send("quit")
        try:
            p.terminate()
        except Exception:
            pass
        if not best_line:
            return {"ok": False, "error": "Stockfish returned no bestmove", "lines": lines[-10:]}
        parts = best_line.split()
        best = parts[1]
        with state.lock:
            state.stockfish_status = "ok"
        return {"ok": True, "bestmove": best, "line": best_line}
    except Exception as e:
        try:
            p.kill()
        except Exception:
            pass
        with state.lock:
            state.stockfish_status = f"error: {e}"
        return {"ok": False, "error": str(e)}


def mjpeg_stream(kind: str = "annotated"):
    while state.running:
        with state.lock:
            frame = state.raw_jpg if kind == "raw" else state.annotated_jpg
        if frame:
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
        time.sleep(0.05)


INDEX_HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Remote Auto Chess Pi</title>
<style>
body { margin:0; background:#111; color:#eee; font-family:Arial, sans-serif; }
header { padding:10px 16px; background:#1d1d1d; border-bottom:1px solid #333; }
main { display:grid; grid-template-columns: 1fr 430px; gap:12px; padding:12px; }
.card { background:#1b1b1b; border:1px solid #333; border-radius:8px; padding:10px; }
#stream { width:100%; max-width:960px; border:1px solid #444; background:#000; cursor:crosshair; }
button, select, input { margin:3px; padding:7px 9px; background:#2d2d2d; color:#eee; border:1px solid #555; border-radius:5px; }
button:hover { background:#3c3c3c; }
pre { white-space:pre-wrap; background:#101010; border:1px solid #333; padding:8px; max-height:230px; overflow:auto; font-size:12px; }
.grid { display:grid; grid-template-columns: repeat(8, 1fr); width:320px; border:1px solid #555; }
.sq { width:40px; height:40px; display:flex; align-items:center; justify-content:center; font-weight:bold; font-size:18px; }
.light { background:#d3c4a3; color:#111; }
.dark { background:#795b3d; color:#fff; }
.small { font-size:12px; color:#aaa; }
.good { color:#7fff7f; } .bad { color:#ff7777; } .warn { color:#ffcc66; }
.row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
</style>
</head>
<body>
<header>
  <b>Remote Auto Chess</b> | Pi camera + Stockfish + Arduino USB serial | no webhooks, local LAN only
</header>
<main>
<section class="card">
  <h3>Live camera / recognition</h3>
  <img id="stream" src="/stream.mjpg" onclick="learnClick(event)">
  <div class="small">For color learning: choose piece type, then click the visible piece color in the stream.</div>
</section>
<aside>
  <section class="card">
    <h3>Controls</h3>
    <div class="row">
      <button onclick="api('/api/scan_human')">Scan human move</button>
      <button onclick="api('/api/engine_move')">Engine move</button>
      <button onclick="api('/api/scan_then_engine')">Scan + engine</button>
      <button onclick="api('/api/reset_game')">Reset game</button>
    </div>
    <div class="row">
      <button onclick="api('/api/serial_ping')">PING Arduino</button>
      <button onclick="api('/api/serial_zero')">ZERO Arduino</button>
      <button onclick="api('/api/serial_stop')">STOP Arduino</button>
    </div>
    <h4>Teach piece color</h4>
    <select id="learnPiece">
      <option value="P">P pawn</option>
      <option value="N">N knight</option>
      <option value="B">B bishop</option>
      <option value="R">R rook</option>
      <option value="Q">Q queen</option>
      <option value="K">K king</option>
    </select>
    <span class="small">click object in stream</span>
  </section>

  <section class="card">
    <h3>Board</h3>
    <div id="board" class="grid"></div>
    <div id="turn" class="small"></div>
  </section>

  <section class="card">
    <h3>Last result</h3>
    <pre id="result">loading...</pre>
  </section>

  <section class="card">
    <h3>Moves</h3>
    <pre id="moves"></pre>
  </section>

  <section class="card">
    <h3>Pi sees / Arduino sends</h3>
    <pre id="status"></pre>
  </section>
</aside>
</main>
<script>
const files = ['a','b','c','d','e','f','g','h'];
function pieceToChar(p){
  const map = {'P':'♙','N':'♘','B':'♗','R':'♖','Q':'♕','K':'♔','p':'♟','n':'♞','b':'♝','r':'♜','q':'♛','k':'♚'};
  return map[p] || p || '';
}
async function getState(){
  const r = await fetch('/api/state');
  const s = await r.json();
  drawBoard(s.board_pieces || {});
  document.getElementById('turn').innerText = 'Turn: ' + s.turn + ' | FEN: ' + s.fen;
  document.getElementById('moves').innerText = (s.move_log || []).map(m => `${m.ply}. ${m.source}: ${m.san} (${m.uci})`).join('\n');
  document.getElementById('status').innerText = JSON.stringify({
    serial_status:s.serial_status,
    stockfish_status:s.stockfish_status,
    observed_types:s.observed_types,
    detections:s.detections,
    candidates:s.candidate_moves,
    arduino_log:s.arduino_log,
    app_log:s.app_log.slice(-20),
    last_error:s.last_error
  }, null, 2);
}
function drawBoard(pieces){
  const b = document.getElementById('board');
  b.innerHTML = '';
  for(let rank=8; rank>=1; rank--){
    for(let fi=0; fi<8; fi++){
      const sq = files[fi] + rank;
      const d = document.createElement('div');
      d.className = 'sq ' + (((rank+fi)%2)?'light':'dark');
      d.title = sq;
      d.textContent = pieceToChar(pieces[sq]);
      b.appendChild(d);
    }
  }
}
async function api(path){
  const r = await fetch(path, {method:'POST'});
  const j = await r.json();
  document.getElementById('result').innerText = JSON.stringify(j,null,2);
  await getState();
}
async function learnClick(ev){
  const img = document.getElementById('stream');
  const rect = img.getBoundingClientRect();
  const x = Math.round((ev.clientX - rect.left) * (img.naturalWidth || 960) / rect.width);
  const y = Math.round((ev.clientY - rect.top) * (img.naturalHeight || 720) / rect.height);
  const piece = document.getElementById('learnPiece').value;
  const r = await fetch('/api/learn_color', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({piece_type:piece, x:x, y:y})
  });
  const j = await r.json();
  document.getElementById('result').innerText = JSON.stringify(j,null,2);
  await getState();
}
setInterval(getState, 1500);
getState();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return INDEX_HTML


@app.route("/stream.mjpg")
def stream():
    return Response(mjpeg_stream("annotated"), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/raw.mjpg")
def raw_stream():
    return Response(mjpeg_stream("raw"), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/raw.jpg")
def raw_jpg():
    with state.lock:
        frame = state.raw_jpg
    if not frame:
        return "No frame", 503
    return Response(frame, mimetype="image/jpeg")


@app.route("/api/state")
def api_state():
    with state.lock:
        return jsonify({
            "fen": state.board.fen(),
            "turn": "white" if state.board.turn == chess.WHITE else "black",
            "board_pieces": board_piece_map_text(state.board),
            "observed_types": state.observed_types,
            "detections": state.detections,
            "move_log": state.move_log[-80:],
            "candidate_moves": state.candidate_moves,
            "serial_status": state.serial_status,
            "stockfish_status": state.stockfish_status,
            "arduino_log": state.arduino_log[-50:],
            "app_log": state.app_log[-80:],
            "last_error": state.last_error,
            "config_path": str(CONFIG_PATH)
        })


@app.route("/api/learn_color", methods=["POST"])
def api_learn_color():
    data = request.get_json(force=True)
    piece_type = str(data.get("piece_type", "")).upper()
    if piece_type not in ["P", "N", "B", "R", "Q", "K"]:
        return jsonify({"ok": False, "error": "piece_type must be P/N/B/R/Q/K"}), 400
    x = int(data.get("x", -1))
    y = int(data.get("y", -1))
    with state.lock:
        frame = None if state.raw_frame_bgr is None else state.raw_frame_bgr.copy()
    if frame is None:
        return jsonify({"ok": False, "error": "no camera frame yet"}), 503
    h, w = frame.shape[:2]
    if x < 0 or y < 0 or x >= w or y >= h:
        return jsonify({"ok": False, "error": f"point outside frame {w}x{h}", "x": x, "y": y}), 400
    r = int(config["vision"].get("learn_patch_radius", 10))
    x1, x2 = max(0, x - r), min(w, x + r + 1)
    y1, y2 = max(0, y - r), min(h, y + r + 1)
    patch = frame[y1:y2, x1:x2]
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV).reshape(-1, 3)
    mean = np.mean(hsv, axis=0).astype(int).tolist()
    h0, s0, v0 = mean
    ranges = hue_ranges_from_sample(h0, s0, v0)
    config["piece_colors_hsv"][piece_type] = ranges
    # Draw label color from sampled BGR.
    bgr = np.mean(patch.reshape(-1, 3), axis=0).astype(int).tolist()
    config["draw_colors_bgr"][piece_type] = bgr
    save_config(config)
    add_log(f"learned {piece_type} from point {x},{y}: HSV {mean}")
    return jsonify({"ok": True, "piece_type": piece_type, "hsv_mean": mean, "ranges": ranges, "bgr": bgr})


@app.route("/api/scan_human", methods=["POST"])
def api_scan_human():
    with state.lock:
        if state.board.turn != chess.WHITE:
            return jsonify({"ok": False, "error": "not white/human turn"}), 400
    candidates = legal_move_candidates_from_observation()
    with state.lock:
        state.candidate_moves = candidates
    if not candidates:
        return jsonify({"ok": False, "error": "no legal candidates"})
    best = candidates[0]
    clear_score = int(config["game"].get("scan_clear_score", 6))
    margin = int(config["game"].get("scan_margin", 2))
    second = candidates[1]["score"] if len(candidates) > 1 else 999
    if best["score"] <= clear_score and second - best["score"] >= margin:
        res = apply_move_uci(best["uci"], "human-scan", send_to_arduino=False)
        return jsonify({"ok": True, "applied": True, "best": best, "result": res, "candidates": candidates})
    return jsonify({"ok": True, "applied": False, "best": best, "candidates": candidates, "note": "ambiguous; improve color/board calibration or confirm manually"})


@app.route("/api/confirm_move", methods=["POST"])
def api_confirm_move():
    data = request.get_json(force=True)
    uci = data.get("uci", "")
    return jsonify(apply_move_uci(uci, "manual-confirm", send_to_arduino=False))


@app.route("/api/engine_move", methods=["POST"])
def api_engine_move():
    with state.lock:
        if state.board.turn != chess.BLACK:
            return jsonify({"ok": False, "error": "not black/engine turn"}), 400
        fen = state.board.fen()
    sf = stockfish_best_move(fen)
    if not sf.get("ok"):
        return jsonify(sf), 500
    res = apply_move_uci(sf["bestmove"], "stockfish", send_to_arduino=True)
    return jsonify({"ok": bool(res.get("ok")), "stockfish": sf, "result": res})


@app.route("/api/scan_then_engine", methods=["POST"])
def api_scan_then_engine():
    scan_resp = api_scan_human().get_json()
    if not scan_resp.get("ok") or not scan_resp.get("applied"):
        return jsonify({"ok": False, "scan": scan_resp, "engine": None})
    eng_response = api_engine_move()
    try:
        eng_json = eng_response.get_json()
    except Exception:
        eng_json = {"ok": False, "error": "engine response parse failed"}
    return jsonify({"ok": bool(eng_json.get("ok")), "scan": scan_resp, "engine": eng_json})


@app.route("/api/reset_game", methods=["POST"])
def api_reset_game():
    with state.lock:
        state.board = chess.Board()
        state.move_log = []
        state.candidate_moves = []
    add_log("game reset to standard starting position")
    return jsonify({"ok": True, "fen": state.board.fen()})


@app.route("/api/serial_ping", methods=["POST"])
def api_serial_ping():
    return jsonify(arduino.send("PING"))


@app.route("/api/serial_zero", methods=["POST"])
def api_serial_zero():
    return jsonify(arduino.send("ZERO"))


@app.route("/api/serial_stop", methods=["POST"])
def api_serial_stop():
    return jsonify(arduino.send("STOP"))


@app.route("/api/send_serial", methods=["POST"])
def api_send_serial():
    data = request.get_json(force=True)
    cmd = str(data.get("cmd", ""))
    return jsonify(arduino.send(cmd))


def startup_checks() -> None:
    # Do not hard-fail if Arduino/Stockfish missing; the UI should still start.
    arduino.connect()
    try:
        r = subprocess.run([config["stockfish"].get("path", "stockfish")], input="uci\nquit\n", text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
        with state.lock:
            state.stockfish_status = "found" if "Stockfish" in r.stdout or "uciok" in r.stdout else "unknown response"
    except Exception as e:
        with state.lock:
            state.stockfish_status = f"not found/test failed: {e}"


def main() -> None:
    add_log("Remote Auto Chess Pi starting")
    add_log(f"config: {CONFIG_PATH}")
    startup_checks()
    t = threading.Thread(target=camera_loop, daemon=True)
    t.start()
    host = config["server"].get("host", "0.0.0.0")
    port = int(config["server"].get("port", 5000))
    add_log(f"web server: http://{host}:{port}")
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        state.running = False
        add_log("stopped")
