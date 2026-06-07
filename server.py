#!/usr/bin/env python3
"""Voice Inject Server — optimized for Command Mode only."""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import yaml
import sys
import logging
from pathlib import Path
import json

def _load_dotenv():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

import os
_load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ],
    force=True
)
logger = logging.getLogger(__name__)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CONFIG_DIR = Path.home() / ".voice-inject"
CONFIG_PATH = CONFIG_DIR / "config.yaml"
CONFIG_DIR.mkdir(exist_ok=True)

active_connections = []
warmup_state = {"type": "warmup_started"}

def load_config():
    defaults = {"min_speech_energy": 180, "active_preset": "office"}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            data = yaml.safe_load(f) or {}
            defaults.update(data)
    return defaults

def save_config(config: dict):
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global warmup_state
    await websocket.accept()
    active_connections.append(websocket)
    await websocket.send_text(json.dumps(warmup_state))
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            if message.get("type") == "status":
                for connection in active_connections:
                    if connection != websocket:
                        await connection.send_text(data)
            elif message.get("type") in ("warmup_started", "warmup_complete", "warmup_progress"):
                warmup_state = message
                for connection in active_connections:
                    if connection != websocket:
                        await connection.send_text(data)
    except WebSocketDisconnect:
        active_connections.remove(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        if websocket in active_connections:
            active_connections.remove(websocket)

@app.get("/api/config")
async def get_config():
    return load_config()

@app.post("/api/config")
async def update_config(new_config: dict):
    config = load_config()
    config.update(new_config)
    save_config(config)
    for connection in active_connections:
        await connection.send_text(json.dumps({
            "type": "config_updated",
            "config": config
        }))
    return {"success": True}

@app.get("/health")
async def health():
    return {"status": "ok", "service": "voice-inject-server"}

@app.get("/", response_class=HTMLResponse)
async def get_ui():
    html_content = """
<!DOCTYPE html>
<html>
<head>
    <title>Voice Inject</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
        }
        .container {
            background: white;
            border-radius: 20px;
            padding: 40px;
            width: 100%;
            max-width: 500px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
        }
        h1 {
            text-align: center;
            color: #333;
            margin-bottom: 30px;
            font-size: 32px;
        }
        .command-info {
            text-align: center;
            padding: 20px 0;
        }
        .command-icon {
            font-size: 48px;
            margin-bottom: 15px;
            background: #f0f4ff;
            width: 80px;
            height: 80px;
            line-height: 80px;
            border-radius: 50%;
            margin: 0 auto 15px;
            color: #667eea;
        }
        .command-info h2 {
            color: #333;
            margin-bottom: 10px;
        }
        .command-info p {
            color: #666;
            margin-bottom: 25px;
            line-height: 1.5;
        }
        .status-box {
            background: #f9f9f9;
            border-radius: 12px;
            padding: 15px;
            margin-bottom: 25px;
            font-size: 14px;
            color: #555;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
        }
        .config-section {
            background: #f0f4ff;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 20px;
            text-align: left;
        }
        .config-section h3 {
            font-size: 14px;
            color: #667eea;
            margin-bottom: 12px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .preset-grid {
            display: grid;
            grid-template-columns: 1fr 1fr 1fr;
            gap: 10px;
        }
        .preset-btn {
            background: white;
            border: 2px solid #e0e0e0;
            border-radius: 8px;
            padding: 10px 5px;
            cursor: pointer;
            font-size: 12px;
            font-weight: 600;
            color: #666;
            transition: all 0.2s;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 5px;
        }
        .preset-btn span { font-size: 20px; }
        .preset-btn:hover { border-color: #667eea; }
        .preset-btn.active {
            border-color: #667eea;
            background: #667eea;
            color: white;
        }
        .status-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #ccc;
        }
        .status-dot.active { background: #4CAF50; }
        .status-dot.recording { background: #f44336; animation: pulse 1s infinite; }
        @keyframes pulse {
            0% { transform: scale(1); opacity: 1; }
            50% { transform: scale(1.2); opacity: 0.7; }
            100% { transform: scale(1); opacity: 1; }
        }
        .diagnostics {
            margin-top: 20px;
            padding: 15px;
            border-radius: 10px;
            font-size: 13px;
            background: #f5f5f5;
            color: #666;
        }
        .progress-container {
            width: 100%;
            height: 8px;
            background: #eee;
            border-radius: 10px;
            margin-top: 15px;
            overflow: hidden;
            display: none;
        }
        .progress-bar {
            height: 100%;
            background: #667eea;
            width: 0%;
            transition: width 0.3s;
        }
        .warmup-msg {
            font-size: 12px;
            color: #999;
            margin-top: 8px;
            display: none;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎙️ Voice Inject</h1>
        
        <div class="command-info">
            <div class="command-icon">⌥</div>
            <h2>Command Mode</h2>
            <p>Double-tap <b>Left Option</b> to record.<br>Tap again to stop, or just pause to auto-paste.</p>
            
            <div class="status-box">
                <div class="status-dot" id="statusDot"></div>
                <span id="statusText">Connecting...</span>
            </div>

            <div class="config-section">
                <h3>Mic Environment</h3>
                <div class="preset-grid">
                    <button class="preset-btn" id="preset-laptop" onclick="setPreset('laptop', 100)">
                        <span>💻</span> Laptop
                    </button>
                    <button class="preset-btn active" id="preset-office" onclick="setPreset('office', 180)">
                        <span>🏢</span> Office
                    </button>
                    <button class="preset-btn" id="preset-headphones" onclick="setPreset('headphones', 350)">
                        <span>🎧</span> Studio
                    </button>
                </div>
            </div>

            <div class="config-section" id="hfTokenSection" style="display: none;">
                <h3>Hugging Face Token</h3>
                <p style="font-size: 12px; color: #666; margin-bottom: 12px;">A token is required to download gated models like Phi-3.5.</p>
                <div style="display: flex; gap: 10px;">
                    <input type="password" id="hfToken" placeholder="hf_..." style="flex: 1; padding: 10px; border-radius: 8px; border: 2px solid #e0e0e0; font-size: 13px;">
                    <button class="preset-btn" style="width: auto; padding: 0 15px;" onclick="saveToken()">Save</button>
                </div>
                <p style="font-size: 11px; color: #999; margin-top: 8px;">Find yours at <a href="https://huggingface.co/settings/tokens" target="_blank" style="color: #667eea;">hf.co/settings/tokens</a></p>
            </div>

            <div class="progress-container" id="progressContainer">
                <div class="progress-bar" id="progressBar"></div>
            </div>
            <div class="warmup-msg" id="warmupMsg"></div>
        </div>

        <div class="diagnostics" id="diagnostics">
            Waiting for client connection...
        </div>
    </div>
    
    <script>
        let ws = null;
        const statusDot = document.getElementById('statusDot');
        const statusText = document.getElementById('statusText');
        const diagnostics = document.getElementById('diagnostics');
        const progressContainer = document.getElementById('progressContainer');
        const progressBar = document.getElementById('progressBar');
        const warmupMsg = document.getElementById('warmupMsg');
        const hfTokenInput = document.getElementById('hfToken');
        const hfTokenSection = document.getElementById('hfTokenSection');

        async function saveToken() {
            const token = hfTokenInput.value.trim();
            if (!token) return;
            try {
                await fetch('/api/config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ huggingface_token: token })
                });
                hfTokenSection.style.display = 'none';
                alert("Token saved!");
            } catch (e) {
                console.error("Failed to save token", e);
            }
        }

        async function setPreset(id, energy) {
            // Update UI
            document.querySelectorAll('.preset-btn').forEach(btn => btn.classList.remove('active'));
            document.getElementById('preset-' + id).classList.add('active');
            
            // Save to server
            try {
                await fetch('/api/config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        min_speech_energy: energy,
                        active_preset: id
                    })
                });
            } catch (e) {
                console.error("Failed to save config", e);
            }
        }

        async function loadInitialConfig() {
            try {
                const r = await fetch('/api/config');
                const config = await r.json();
                if (config.active_preset) {
                    document.querySelectorAll('.preset-btn').forEach(btn => btn.classList.remove('active'));
                    const btn = document.getElementById('preset-' + config.active_preset);
                    if (btn) btn.classList.add('active');
                }
                if (config.huggingface_token) {
                    hfTokenInput.value = config.huggingface_token;
                } else {
                    hfTokenSection.style.display = 'block';
                }
            } catch (e) {}
        }

        function connect() {
            ws = new WebSocket('ws://' + window.location.host + '/ws');
            ws.onopen = () => {
                statusText.innerText = 'Ready';
                statusDot.className = 'status-dot active';
                diagnostics.innerText = 'Server connected. Waiting for client...';
                loadInitialConfig();
            };
            ws.onmessage = (e) => {
                const msg = JSON.parse(e.data);
                if (msg.type === 'warmup_started') {
                    statusText.innerText = 'Warming up...';
                    statusDot.className = 'status-dot';
                    progressContainer.style.display = 'block';
                    warmupMsg.style.display = 'block';
                } else if (msg.type === 'warmup_progress') {
                    statusText.innerText = 'Warming up...';
                    statusDot.className = 'status-dot';
                    progressContainer.style.display = 'block';
                    warmupMsg.style.display = 'block';
                    progressBar.style.width = msg.percent + '%';
                    warmupMsg.innerText = msg.message || 'Loading models...';
                } else if (msg.type === 'warmup_complete') {
                    statusText.innerText = 'Ready';
                    statusDot.className = 'status-dot active';
                    progressBar.style.width = '100%';
                    setTimeout(() => {
                        progressContainer.style.display = 'none';
                        warmupMsg.style.display = 'none';
                    }, 1000);
                } else if (msg.type === 'status' && msg.mode === 'command') {
                    if (msg.recording) {
                        statusText.innerText = 'Recording...';
                        statusDot.className = 'status-dot recording';
                    } else {
                        statusText.innerText = 'Ready';
                        statusDot.className = 'status-dot active';
                    }
                    diagnostics.innerText = 'Client active. Double-tap Option to start.';
                }
            };
            ws.onclose = () => {
                statusText.innerText = 'Disconnected';
                statusDot.className = 'status-dot';
                setTimeout(connect, 2000);
            };
        }
        connect();
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3000, log_level="info")
