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
VOCAB_PATH = CONFIG_DIR / "vocabulary.json"
SNIPPETS_PATH = CONFIG_DIR / "snippets.json"
CONFIG_DIR.mkdir(exist_ok=True)

active_connections = []
warmup_state = {"type": "warmup_started"}

def load_config():
    defaults = {"min_speech_energy": 180, "mic_type": "builtin", "environment": "normal", "input_gain": 0}
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
            # Broadcast all messages to all connections (client <-> UI)
            for connection in active_connections:
                if connection != websocket:
                    await connection.send_text(data)
            
            if message.get("type") in ("warmup_started", "warmup_complete", "warmup_progress"):
                warmup_state = message
                
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

@app.get("/api/vocabulary")
async def get_vocabulary():
    if VOCAB_PATH.exists():
        with open(VOCAB_PATH) as f:
            return json.load(f)
    return {"entries": []}

@app.post("/api/vocabulary")
async def update_vocabulary(data: dict):
    with open(VOCAB_PATH, "w") as f:
        json.dump(data, f)
    return {"success": True}

@app.get("/api/snippets")
async def get_snippets():
    if SNIPPETS_PATH.exists():
        with open(SNIPPETS_PATH) as f:
            return json.load(f)
    return {"entries": []}

@app.post("/api/snippets")
async def update_snippets(data: dict):
    with open(SNIPPETS_PATH, "w") as f:
        json.dump(data, f)
    return {"success": True}

@app.get("/health")
async def health():
    return {"status": "ok", "service": "voice-inject-server"}

@app.get("/", response_class=HTMLResponse)
async def get_ui():
    html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <title>Voice Inject</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #0a0a0c;
            --container-bg: #141417;
            --accent: #6366f1;
            --accent-soft: rgba(99, 102, 241, 0.1);
            --text-primary: #ffffff;
            --text-secondary: #94a3b8;
            --border: #27272a;
            --success: #10b981;
            --error: #ef4444;
            --radius: 16px;
        }

        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            font-family: 'Inter', -apple-system, system-ui, sans-serif;
            background-color: var(--bg);
            color: var(--text-primary);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
            letter-spacing: -0.01em;
        }

        /* Subtle background glow */
        body::before {
            content: '';
            position: fixed;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            width: 600px;
            height: 600px;
            background: radial-gradient(circle, var(--accent-soft) 0%, transparent 70%);
            z-index: -1;
            pointer-events: none;
        }

        .container {
            background: var(--container-bg);
            border: 1px solid var(--border);
            border-radius: 24px;
            width: 100%;
            max-width: 480px;
            box-shadow: 0 40px 100px rgba(0,0,0,0.5);
            position: relative;
            overflow: hidden;
            backdrop-filter: blur(20px);
        }

        .header {
            padding: 32px 32px 24px;
            text-align: center;
        }

        h1 { 
            font-size: 24px; 
            font-weight: 700; 
            margin-bottom: 8px;
            background: linear-gradient(to bottom right, #fff, #a1a1aa);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .header p {
            font-size: 14px;
            color: var(--text-secondary);
        }

        /* Loading overlay */
        .loading-overlay {
            position: absolute;
            inset: 0;
            background: var(--container-bg);
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            z-index: 100;
            transition: all 0.6s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .loading-overlay.hidden { 
            opacity: 0; 
            pointer-events: none; 
            transform: scale(1.05);
        }

        .spinner-ring {
            width: 64px;
            height: 64px;
            border: 3px solid var(--border);
            border-top-color: var(--accent);
            border-radius: 50%;
            animation: spin 1s cubic-bezier(0.4, 0, 0.2, 1) infinite;
            margin-bottom: 32px;
        }
        @keyframes spin { to { transform: rotate(360deg); } }

        .loading-overlay h2 { font-size: 18px; margin-bottom: 8px; }
        .loading-overlay p { color: var(--text-secondary); font-size: 13px; margin-bottom: 24px; }
        .loading-tip { margin-top: 24px; font-size: 12px; opacity: 0.6; margin-bottom: 0; }

        .progress-container {
            width: 240px; height: 4px; background: var(--border);
            border-radius: 10px; overflow: hidden;
        }
        .progress-bar {
            height: 100%; background: var(--accent); width: 0%; transition: width 0.4s;
            box-shadow: 0 0 20px var(--accent);
        }

        /* Tabs */
        .tab-bar {
            display: flex;
            padding: 0 32px;
            gap: 24px;
            border-bottom: 1px solid var(--border);
        }
        .tab-btn {
            padding: 16px 0;
            border: none;
            background: none;
            font-size: 14px;
            font-weight: 500;
            color: var(--text-secondary);
            cursor: pointer;
            transition: all 0.3s;
            border-bottom: 2px solid transparent;
            position: relative;
        }
        .tab-btn.active { 
            color: var(--text-primary); 
        }
        .tab-btn.active::after {
            content: '';
            position: absolute;
            bottom: -1px;
            left: 0;
            right: 0;
            height: 2px;
            background: var(--accent);
            box-shadow: 0 0 10px var(--accent);
        }

        .tab-panel { display: none; padding: 32px; }
        .tab-panel.active { display: block; animation: fadeIn 0.4s ease-out; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }

        /* Main Display */
        .hero-icon {
            width: 80px; height: 80px;
            background: var(--accent-soft);
            border-radius: 20px;
            margin: 0 auto 24px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: var(--accent);
        }

        .hero-text { text-align: center; margin-bottom: 32px; }
        .hero-text h2 { font-size: 20px; margin-bottom: 8px; }
        .hero-text p { font-size: 14px; color: var(--text-secondary); line-height: 1.6; }

        .status-pill {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 8px 16px;
            background: var(--border);
            border-radius: 100px;
            font-size: 13px;
            font-weight: 500;
            margin-bottom: 32px;
            transition: all 0.3s;
        }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-secondary); transition: all 0.3s; }
        .status-dot.active { background: var(--success); box-shadow: 0 0 10px var(--success); }
        .status-dot.recording { background: var(--error); box-shadow: 0 0 10px var(--error); animation: pulse 1s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }

        /* Settings Sections */
        .section { margin-bottom: 32px; }
        .section-header {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 16px;
            color: var(--text-secondary);
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .card-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }
        .card-btn {
            background: var(--border);
            border: 1px solid transparent;
            border-radius: var(--radius);
            padding: 16px 12px;
            cursor: pointer;
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 8px;
            color: var(--text-secondary);
        }
        .card-btn svg { width: 20px; height: 20px; }
        .card-btn span { font-size: 12px; font-weight: 500; }
        .card-btn:hover { background: #2d2d30; color: var(--text-primary); }
        .card-btn.active { 
            background: var(--accent-soft); 
            border-color: var(--accent); 
            color: var(--accent);
        }

        /* Inputs & Buttons */
        .input-group { display: flex; gap: 8px; margin-bottom: 12px; }
        input {
            flex: 1;
            background: var(--border);
            border: 1px solid transparent;
            border-radius: 12px;
            padding: 12px 16px;
            color: var(--text-primary);
            font-family: inherit;
            font-size: 14px;
            transition: all 0.2s;
        }
        input:focus { outline: none; border-color: var(--accent); background: #2d2d30; }
        
        .action-btn {
            background: var(--accent);
            color: white;
            border: none;
            border-radius: 12px;
            padding: 12px 24px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
        }
        .action-btn:hover { transform: translateY(-1px); box-shadow: 0 10px 20px rgba(99, 102, 241, 0.3); }
        .action-btn:active { transform: translateY(0); }
        .action-btn.secondary { background: var(--border); color: var(--text-primary); }
        .action-btn.secondary:hover { background: #2d2d30; box-shadow: none; }

        .diagnostics {
            margin-top: 8px;
            padding: 16px;
            background: #1a1a1e;
            border-radius: 12px;
            font-family: monospace;
            font-size: 12px;
            color: var(--text-secondary);
            border: 1px solid var(--border);
        }

        .vocab-row, .snippet-row {
            display: flex; gap: 8px; margin-bottom: 8px; align-items: center;
            animation: slideIn 0.3s ease-out;
        }
        @keyframes slideIn { from { opacity: 0; transform: translateX(-10px); } to { opacity: 1; transform: translateX(0); } }

        .delete-btn {
            background: none; border: none; color: var(--text-secondary);
            cursor: pointer; padding: 8px; border-radius: 8px;
            transition: all 0.2s;
        }
        .delete-btn:hover { background: rgba(239, 68, 68, 0.1); color: var(--error); }
    </style>
</head>
<body>
    <div class="container">
        <!-- Loading overlay -->
        <div class="loading-overlay" id="loadingOverlay">
            <div class="spinner-ring"></div>
            <h2>Warming Engines</h2>
            <p id="loadingMsg">Initializing local AI models...</p>
            <div class="progress-container">
                <div class="progress-bar" id="progressBar"></div>
            </div>
            <p class="loading-tip">Tip: A dedicated microphone significantly improves transcription accuracy.</p>
        </div>

        <div class="header">
            <h1>Voice Inject</h1>
            <p>High-fidelity local dictation</p>
        </div>

        <div class="tab-bar">
            <button class="tab-btn active" onclick="switchTab('record')">Dashboard</button>
            <button class="tab-btn" onclick="switchTab('settings')">Settings</button>
        </div>

        <!-- RECORD TAB -->
        <div class="tab-panel active" id="tab-record" style="text-align: center;">
            <div class="hero-icon">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"></path>
                    <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
                    <line x1="12" x2="12" y1="19" y2="22"></line>
                </svg>
            </div>
            
            <div class="hero-text">
                <h2>Command Ready</h2>
                <p>Double-tap <b>Left Option</b> to record.<br>Automatic punctuation and grammar fixes applied.</p>
            </div>

            <div class="status-pill">
                <div class="status-dot" id="statusDot"></div>
                <span id="statusText">Connecting...</span>
            </div>

            <div class="section">
                <div class="section-header">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path><path d="M19 10v2a7 7 0 0 1-14 0v-2"></path><line x1="12" y1="19" x2="12" y2="23"></line><line x1="8" y1="23" x2="16" y2="23"></line></svg>
                    Microphone
                </div>
                <div class="card-grid">
                    <button class="card-btn" id="mic-builtin" onclick="setMic('builtin')">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"></rect><line x1="8" y1="21" x2="16" y2="21"></line><line x1="12" y1="17" x2="12" y2="21"></line></svg>
                        <span>Built-in</span>
                    </button>
                    <button class="card-btn" id="mic-headphones" onclick="setMic('headphones')">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 18v-6a9 9 0 0 1 18 0v6"></path><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"></path></svg>
                        <span>Headset</span>
                    </button>
                    <button class="card-btn" id="mic-external" onclick="setMic('external')">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path><path d="M19 10v2a7 7 0 0 1-14 0v-2"></path><line x1="12" y1="19" x2="12" y2="23"></line></svg>
                        <span>Studio</span>
                    </button>
                </div>
            </div>

            <div class="section">
                <div class="section-header">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
                    Environment
                </div>
                <div class="card-grid">
                    <button class="card-btn" id="env-quiet" onclick="setEnv('quiet')">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 10v3"></path><path d="M6 6v11"></path><path d="M10 3v18"></path><path d="M14 8v7"></path><path d="M18 5v13"></path><path d="M22 10v3"></path></svg>
                        <span>Quiet</span>
                    </button>
                    <button class="card-btn" id="env-normal" onclick="setEnv('normal')">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><line x1="9" y1="3" x2="9" y2="21"></line></svg>
                        <span>Normal</span>
                    </button>
                    <button class="card-btn" id="env-noisy" onclick="setEnv('noisy')">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 5L6 9H2v6h4l5 4V5z"></path><path d="M19.07 4.93a10 10 0 0 1 0 14.14"></path><path d="M15.54 8.46a5 5 0 0 1 0 7.07"></path></svg>
                        <span>Noisy</span>
                    </button>
                </div>
            </div>

            <div class="diagnostics" id="diagnostics">
                Waiting for client link...
            </div>
        </div>

        <!-- SETTINGS TAB -->
        <div class="tab-panel" id="tab-settings">
            <div class="section">
                <div class="section-header">Vocabulary & Phonetics</div>
                <div id="vocabList" style="margin-bottom: 16px;"></div>
                <div style="display: flex; gap: 12px;">
                    <button class="action-btn secondary" style="flex: 1" onclick="addVocabRow()">+ Add Row</button>
                    <button class="action-btn" style="flex: 1" id="saveVocabBtn" onclick="saveVocab()">Save All</button>
                </div>
            </div>

            <div class="section">
                <div class="section-header">Voice Debugger</div>
                <div style="background: rgba(255,255,255,0.03); padding: 20px; border-radius: var(--radius); border: 1px dashed var(--border);">
                    <div style="display: flex; gap: 16px; align-items: center;">
                        <button class="action-btn" id="testMicBtn" style="background: #2d2d30;" onclick="toggleTestMic()">Test Microphone</button>
                        <div id="testResult" style="font-family: monospace; font-size: 14px; color: var(--accent); font-weight: 600;"></div>
                    </div>
                </div>
            </div>

            <div class="section">
                <div class="section-header">Text Snippets</div>
                <div id="snippetList" style="margin-bottom: 16px;"></div>
                <div style="display: flex; gap: 12px;">
                    <button class="action-btn secondary" style="flex: 1" onclick="addSnippetRow()">+ Add Snippet</button>
                    <button class="action-btn" style="flex: 1" id="saveSnippetsBtn" onclick="saveSnippets()">Save All</button>
                </div>
            </div>

            <div class="section" id="hfTokenSection" style="display: none;">
                <div class="section-header">Hugging Face Auth</div>
                <div class="input-group">
                    <input type="password" id="hfToken" placeholder="hf_...">
                    <button class="action-btn" onclick="saveToken()">Save</button>
                </div>
                <p style="font-size: 11px; color: var(--text-secondary); margin-top: 8px;">Find yours at <a href="https://huggingface.co/settings/tokens" target="_blank" style="color: var(--accent); text-decoration: none;">hf.co/settings/tokens</a></p>
            </div>
        </div>
    </div>

    <script>
        let ws = null;
        let isTestingMic = false;
        let modelsReady = false;

        const loadingOverlay = document.getElementById('loadingOverlay');
        const loadingMsg = document.getElementById('loadingMsg');
        const progressBar = document.getElementById('progressBar');
        const statusDot = document.getElementById('statusDot');
        const statusText = document.getElementById('statusText');
        const diagnostics = document.getElementById('diagnostics');
        const hfTokenInput = document.getElementById('hfToken');
        const hfTokenSection = document.getElementById('hfTokenSection');
        const vocabList = document.getElementById('vocabList');
        const snippetList = document.getElementById('snippetList');
        const testMicBtn = document.getElementById('testMicBtn');
        const testResult = document.getElementById('testResult');

        function switchTab(id) {
            document.querySelectorAll('.tab-btn').forEach((btn, i) => {
                btn.classList.toggle('active', i === (id === 'record' ? 0 : 1));
            });
            document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
            document.getElementById('tab-' + id).classList.add('active');
        }

        function addVocabRow(word = '', hint = '') {
            const div = document.createElement('div');
            div.className = 'vocab-row';
            div.innerHTML = `
                <input type="text" placeholder="Word" value="${word}">
                <input type="text" placeholder="Phonetic" value="${hint}">
                <button class="delete-btn" onclick="this.parentElement.remove()"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"></path><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"></path><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"></path></svg></button>
            `;
            vocabList.appendChild(div);
        }

        function addSnippetRow(trigger = '', text = '') {
            const div = document.createElement('div');
            div.className = 'snippet-row';
            div.innerHTML = `
                <input type="text" placeholder="Trigger" value="${trigger}">
                <input type="text" placeholder="Expansion" value="${text}">
                <button class="delete-btn" onclick="this.parentElement.remove()"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"></path><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"></path><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"></path></svg></button>
            `;
            snippetList.appendChild(div);
        }

        async function saveVocab() {
            const entries = [];
            vocabList.querySelectorAll('.vocab-row').forEach(row => {
                const inputs = row.querySelectorAll('input');
                const word = inputs[0].value.trim();
                const hint = inputs[1].value.trim();
                if (word) entries.push({ word, hint });
            });
            try {
                const btn = document.getElementById('saveVocabBtn');
                btn.innerText = 'Saving...';
                await fetch('/api/vocabulary', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ entries }) });
                btn.innerText = 'Success';
                setTimeout(() => btn.innerText = 'Save All', 2000);
            } catch (e) { console.error(e); }
        }

        async function saveSnippets() {
            const entries = [];
            snippetList.querySelectorAll('.snippet-row').forEach(row => {
                const inputs = row.querySelectorAll('input');
                const trigger = inputs[0].value.trim();
                const text = inputs[1].value.trim();
                if (trigger && text) entries.push({ trigger, text });
            });
            try {
                const btn = document.getElementById('saveSnippetsBtn');
                btn.innerText = 'Saving...';
                await fetch('/api/snippets', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ entries }) });
                btn.innerText = 'Success';
                setTimeout(() => btn.innerText = 'Save All', 2000);
            } catch (e) { console.error(e); }
        }

        function toggleTestMic() {
            isTestingMic = !isTestingMic;
            if (isTestingMic) {
                testMicBtn.innerText = 'Listening...';
                testMicBtn.style.background = 'var(--error)';
                testResult.innerText = '';
                ws.send(JSON.stringify({ type: 'test_mic_start' }));
            } else {
                testMicBtn.innerText = 'Test Microphone';
                testMicBtn.style.background = '#2d2d30';
                ws.send(JSON.stringify({ type: 'test_mic_stop' }));
            }
        }

        async function saveToken() {
            const token = hfTokenInput.value.trim();
            if (!token) return;
            try {
                await fetch('/api/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ huggingface_token: token }) });
                hfTokenSection.style.display = 'none';
            } catch (e) { console.error(e); }
        }

        const MIC_GAIN = { builtin: 1.0, headphones: 20.0, external: 1.0 };
        const ENV_ENERGY = { quiet: 80, normal: 180, noisy: 350 };

        async function setMic(id) {
            document.querySelectorAll('[id^="mic-"]').forEach(btn => btn.classList.remove('active'));
            document.getElementById('mic-' + id).classList.add('active');
            await saveAudioConfig(id, null);
        }

        async function setEnv(id) {
            document.querySelectorAll('[id^="env-"]').forEach(btn => btn.classList.remove('active'));
            document.getElementById('env-' + id).classList.add('active');
            await saveAudioConfig(null, id);
        }

        async function saveAudioConfig(mic, env) {
            const activeMic = mic || document.querySelector('[id^="mic-"].active')?.id.replace('mic-', '') || 'builtin';
            const activeEnv = env || document.querySelector('[id^="env-"].active')?.id.replace('env-', '') || 'normal';
            try {
                await fetch('/api/config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ mic_type: activeMic, environment: activeEnv, input_gain: MIC_GAIN[activeMic], min_speech_energy: ENV_ENERGY[activeEnv] })
                });
            } catch (e) { console.error(e); }
        }

        async function loadInitialConfig() {
            try {
                const r = await fetch('/api/config');
                const config = await r.json();

                const mic = config.mic_type || 'builtin';
                document.querySelectorAll('[id^="mic-"]').forEach(btn => btn.classList.remove('active'));
                const micBtn = document.getElementById('mic-' + mic);
                if (micBtn) micBtn.classList.add('active');

                const env = config.environment || 'normal';
                document.querySelectorAll('[id^="env-"]').forEach(btn => btn.classList.remove('active'));
                const envBtn = document.getElementById('env-' + env);
                if (envBtn) envBtn.classList.add('active');

                if (config.huggingface_token) {
                    hfTokenInput.value = config.huggingface_token;
                } else {
                    hfTokenSection.style.display = 'block';
                }

                const vr = await fetch('/api/vocabulary');
                const vdata = await vr.json();
                vocabList.innerHTML = '';
                (vdata.entries && vdata.entries.length > 0) ? vdata.entries.forEach(e => addVocabRow(e.word, e.hint)) : addVocabRow();

                const sr = await fetch('/api/snippets');
                const sdata = await sr.json();
                snippetList.innerHTML = '';
                (sdata.entries && sdata.entries.length > 0) ? sdata.entries.forEach(e => addSnippetRow(e.trigger, e.text)) : addSnippetRow();
            } catch (e) {}
        }

        function connect() {
            ws = new WebSocket('ws://' + window.location.host + '/ws');
            ws.onopen = () => {
                statusText.innerText = 'Connected';
                diagnostics.innerText = 'System ready. Waiting for input...';
                loadInitialConfig();
            };
            ws.onmessage = (e) => {
                const msg = JSON.parse(e.data);
                if (msg.type === 'warmup_started' || msg.type === 'warmup_progress') {
                    loadingMsg.innerText = msg.message || 'Loading models...';
                    if (msg.percent) progressBar.style.width = msg.percent + '%';
                } else if (msg.type === 'warmup_complete') {
                    modelsReady = true;
                    progressBar.style.width = '100%';
                    setTimeout(() => loadingOverlay.classList.add('hidden'), 500);
                    statusText.innerText = 'Ready';
                    statusDot.className = 'status-dot active';
                } else if (msg.type === 'status' && msg.mode === 'command') {
                    if (msg.recording) {
                        statusText.innerText = 'Recording';
                        statusDot.className = 'status-dot recording';
                    } else {
                        statusText.innerText = 'Ready';
                        statusDot.className = 'status-dot active';
                    }
                } else if (msg.type === 'test_mic_result') {
                    testResult.innerText = '"' + msg.text + '"';
                    if (isTestingMic) toggleTestMic();
                }
            };
            ws.onclose = () => {
                statusText.innerText = 'Offline';
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
