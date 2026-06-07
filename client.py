#!/usr/bin/env python3
"""Voice Inject Client — optimized for Command Mode (auto-paste) only."""

import subprocess
import signal
import sys
import os
import logging
import sounddevice as sd
from pynput import keyboard
import numpy as np
import time
import asyncio
import websockets
import json
import threading
import queue
from pathlib import Path

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

_load_dotenv()

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHANNELS = 1

# Global state
command_recording = False     # Command mode: double-tap Left Option
command_buffer = []           # Audio buffer for command mode
last_option_press = 0
DOUBLE_TAP_THRESHOLD = 0.6

# Message queue for WebSocket
message_queue = queue.Queue()
ws_connected = False
_warmup_done = False

_MLX_MODEL = "mlx-community/whisper-medium-mlx"
_LLM_MODEL = "mlx-community/Phi-3.5-mini-instruct-4bit"

_WHISPER_HALLUCINATIONS = {
    "thank you", "thank you.", "thanks.", "thanks for watching.",
    "thanks for watching", "thank you for watching.",
    "thank you for watching", "you", "bye.", "bye",
    "the end.", "the end", "subscribe.", "like and subscribe.",
    "more paste.", "more paste", "thanks for watching!", "thank you for watching!"
}

import yaml

def get_config_setting(key, default):
    """Load a setting from ~/.voice-inject/config.yaml, falling back to ENV then default."""
    config_path = Path.home() / ".voice-inject" / "config.yaml"
    if config_path.exists():
        try:
            with open(config_path) as f:
                data = yaml.safe_load(f) or {}
                if key in data:
                    return data[key]
        except Exception:
            pass
    return os.environ.get(key.upper(), default)

def get_vocabulary():
    """Load custom vocabulary from ~/.voice-inject/vocabulary.json and format for Whisper."""
    vocab_path = Path.home() / ".voice-inject" / "vocabulary.json"
    if vocab_path.exists():
        try:
            with open(vocab_path) as f:
                data = json.load(f)
                entries = data.get("entries", [])
                formatted = []
                for e in entries:
                    word = e.get("word", "").strip()
                    hint = e.get("hint", "").strip()
                    if word:
                        if hint:
                            formatted.append(f"{word} ({hint})")
                        else:
                            formatted.append(word)
                return ", ".join(formatted) if formatted else None
        except Exception:
            pass
    return None

# --- AUDIO CUES ---
def play_cue(frequency=800, duration=0.1):
    """Play a short subtle sine-wave beep."""
    try:
        t = np.linspace(0, duration, int(SAMPLE_RATE * duration), False)
        wave = 0.1 * np.sin(2 * np.pi * frequency * t)
        sd.play(wave, SAMPLE_RATE)
    except:
        pass

# === MLX WORKER THREAD ===

mlx_request_queue = queue.Queue()
_llm_model = None
_llm_tokenizer = None

def mlx_worker():
    """Dedicated thread for all Whisper and LLM operations."""
    global _llm_model, _llm_tokenizer, _warmup_done
    
    import mlx.core as mx
    import mlx_whisper
    from mlx_lm import load, generate
    
    # Initialize MLX on this thread
    mx.set_default_device(mx.gpu)
    
    while True:
        request = mlx_request_queue.get()
        if request is None: break
        
        req_type = request.get("type")
        callback = request.get("callback")
        
        try:
            # Set HF token if available in config
            hf_token = get_config_setting("huggingface_token", "").strip()
            if hf_token:
                os.environ["HF_TOKEN"] = hf_token

            if req_type == "warmup":
                print("⏳ Warming up models...")
                message_queue.put({"type": "warmup_progress", "percent": 10, "message": "Starting warmup..."})
                
                # Whisper warmup
                message_queue.put({"type": "warmup_progress", "percent": 20, "message": "Loading Whisper..."})
                silence = np.zeros(16000, dtype=np.float32)
                try:
                    mlx_whisper.transcribe(silence, path_or_hf_repo=_MLX_MODEL, condition_on_previous_text=False)
                    print("✅ Whisper warm")
                except Exception as e:
                    print(f"⚠️ Whisper load failed: {e}")
                    message_queue.put({"type": "warmup_progress", "percent": 20, "message": f"Whisper Error: {str(e)[:50]}"})
                    time.sleep(2) # let user see error

                # LLM warmup
                message_queue.put({"type": "warmup_progress", "percent": 50, "message": "Whisper ready. Loading LLM..."})
                if _llm_model is None:
                    try:
                        _llm_model, _llm_tokenizer = load(_LLM_MODEL)
                        print("✅ LLM warm")
                    except Exception as e:
                        print(f"⚠️ LLM load failed: {e}")
                        message_queue.put({"type": "warmup_progress", "percent": 50, "message": f"LLM Error: {str(e)[:50]}"})
                        time.sleep(2)
                
                message_queue.put({"type": "warmup_progress", "percent": 90, "message": "Models ready. Finalizing..."})
                
                _warmup_done = True
                message_queue.put({"type": "warmup_complete"})
                print("🔥 Models ready\n")

            elif req_type == "transcribe":
                audio = request.get("audio")
                vocab = get_vocabulary()
                result = mlx_whisper.transcribe(
                    audio,
                    path_or_hf_repo=_MLX_MODEL,
                    language="en",
                    condition_on_previous_text=False,
                    initial_prompt=vocab
                )
                text = (result.get("text") or "").strip()
                if text.lower() in _WHISPER_HALLUCINATIONS:
                    text = ""
                if callback: callback(text)

            elif req_type == "cleanup":
                raw_text = request.get("text")
                if not raw_text.strip():
                    if callback: callback(raw_text)
                    continue
                
                if _llm_model is None:
                    _llm_model, _llm_tokenizer = load(_LLM_MODEL)
                
                prompt = f"""<|system|>
You are a speech-to-text post-processor. Your ONLY task is to fix grammar and punctuation.
Do NOT change the wording.
Do NOT change the tone.
Do NOT remove filler words.
Do NOT add any notes or comments.
Do NOT polish or rephrase the text.
Ensure there is a single space after every period, comma, or punctuation mark.
Output the cleaned version only.<|end|>
<|user|>
Fix grammar and punctuation for the following text. Keep all original words and tone:

{raw_text}<|end|>
<|assistant|>
"""
                response = generate(_llm_model, _llm_tokenizer, prompt=prompt, max_tokens=150)
                
                if "<|end|>" in response:
                    response = response.split("<|end|>")[0]
                for stop in ["\n(", "\n\n", "\nNote:", "\n---"]:
                    if stop in response:
                        response = response.split(stop)[0]
                
                if callback: callback(response.strip() or raw_text)

        except Exception as e:
            print(f"⚠️ MLX Worker Error ({req_type}): {e}")
            if callback: callback(None)
        
        mlx_request_queue.task_done()

# Start worker thread
threading.Thread(target=mlx_worker, daemon=True).start()

def audio_callback(indata, frames, time_info, status):
    if command_recording:
        command_buffer.append(indata.copy())

def paste_text(text: str):
    """Copy to clipboard and auto-paste via Cmd+V. Ensures trailing space for flow."""
    if not text:
        return
    
    # Add trailing space if missing for natural flow between segments
    if not text.endswith(" "):
        text += " "
        
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
    except Exception as e:
        print(f"⚠️ Clipboard copy failed: {e}")
        return
    subprocess.run([
        "osascript", "-e",
        'tell application "System Events" to keystroke "v" using command down'
    ], capture_output=True, text=True)

test_mode_active = False

def handle_transcription_result(text: str):
    """Callback from worker thread when transcription is done."""
    global test_mode_active
    if test_mode_active:
        res_text = text if text else "(No speech detected)"
        print(f"🔬 Test Result: {res_text}")
        message_queue.put({"type": "test_mic_result", "text": res_text})
        test_mode_active = False
        return

    if text:
        def handle_cleanup_result(cleaned: str):
            if cleaned:
                print(f"✨ {cleaned}")
                paste_text(cleaned)
        
        mlx_request_queue.put({
            "type": "cleanup",
            "text": text,
            "callback": handle_cleanup_result
        })

def command_vad_loop():
    global command_buffer
    import webrtcvad
    vad = webrtcvad.Vad(3)
    frame_duration_ms = 30
    frame_size = int(SAMPLE_RATE * frame_duration_ms / 1000)
    silence_threshold_frames = int(0.3 * 1000 / frame_duration_ms)
    silence_frames = 0
    has_speech = False
    
    while command_recording:
        time.sleep(0.1)
        if not command_buffer:
            continue
        
        total_audio = np.concatenate(command_buffer, axis=0).flatten()
        if len(total_audio) < frame_size:
            continue
        
        last_frame = total_audio[-frame_size:]
        frame_bytes = last_frame.astype(np.int16).tobytes()
        try:
            is_speech = vad.is_speech(frame_bytes, SAMPLE_RATE)
        except Exception:
            is_speech = True
            
        if is_speech:
            has_speech = True
            silence_frames = 0
        else:
            silence_frames += 1
            
        if has_speech and silence_frames >= silence_threshold_frames:
            captured = command_buffer
            command_buffer = []
            silence_frames = 0
            has_speech = False
            
            audio_data = np.concatenate(captured, axis=0)
            rms = np.sqrt(np.mean(audio_data.astype(np.float64) ** 2))
            
            # Re-read config for real-time UI updates
            min_energy = int(get_config_setting("min_speech_energy", "180"))
            if rms < min_energy:
                continue
                
            audio_float = audio_data.astype(np.float32).flatten() / 32768.0
            mlx_request_queue.put({
                "type": "transcribe",
                "audio": audio_float,
                "callback": handle_transcription_result
            })

def command_flush_remaining():
    global command_buffer
    captured, command_buffer = command_buffer, []
    if not captured:
        return
        
    audio_data = np.concatenate(captured, axis=0)
    rms = np.sqrt(np.mean(audio_data.astype(np.float64) ** 2))
    min_energy = int(get_config_setting("min_speech_energy", "180"))
    if rms < min_energy:
        return
        
    audio_float = audio_data.astype(np.float32).flatten() / 32768.0
    mlx_request_queue.put({
        "type": "transcribe",
        "audio": audio_float,
        "callback": handle_transcription_result
    })
    print("📋 Command mode done.\n")

_command_cooldown = 0
def toggle_command():
    global command_recording, command_buffer, _command_cooldown
    now = time.time()
    if now - _command_cooldown < 1.0:
        return
    if not command_recording:
        command_recording = True
        command_buffer = []
        play_cue(frequency=1000) # High blip for START
        print("🎤 Command mode: recording...")
        message_queue.put({"type": "status", "recording": True, "mode": "command"})
        threading.Thread(target=command_vad_loop, daemon=True).start()
    else:
        command_recording = False
        _command_cooldown = now
        play_cue(frequency=600) # Low blip for STOP
        print("⏹️ Command mode: finishing up...")
        message_queue.put({"type": "status", "recording": False, "mode": "command"})
        threading.Thread(target=command_flush_remaining, daemon=True).start()

def on_press(key):
    global last_option_press
    if key == keyboard.Key.alt_l:
        current_time = time.time()
        if (current_time - last_option_press) < DOUBLE_TAP_THRESHOLD:
            toggle_command()
            last_option_press = 0
        else:
            last_option_press = current_time

async def websocket_client():
    global ws_connected
    uri = "ws://localhost:3000/ws"
    while True:
        try:
            async with websockets.connect(uri) as websocket:
                ws_connected = True
                print("✅ Connected to server\n")
                if _warmup_done:
                    message_queue.put({"type": "warmup_complete"})
                else:
                    message_queue.put({"type": "warmup_progress", "percent": 0, "message": "Waiting for worker..."})
                async def send_messages():
                    while True:
                        try:
                            while not message_queue.empty():
                                msg = message_queue.get_nowait()
                                await websocket.send(json.dumps(msg))
                            await asyncio.sleep(0.1)
                        except Exception:
                            break
                async def receive_messages():
                    global command_recording, test_mode_active
                    async for message in websocket:
                        try:
                            msg = json.loads(message)
                            if msg.get("type") == "test_mic_start":
                                test_mode_active = True
                                if not command_recording:
                                    toggle_command()
                            elif msg.get("type") == "test_mic_stop":
                                if command_recording:
                                    toggle_command()
                        except Exception:
                            pass
                await asyncio.gather(send_messages(), receive_messages(), return_exceptions=True)
        except Exception:
            ws_connected = False
            await asyncio.sleep(5)

def start_websocket_thread():
    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(websocket_client())
    threading.Thread(target=run, daemon=True).start()

def main():
    print("🎙️ Voice Inject (Command Mode Only)")
    print("   Double-tap Left Option ⌥ (transcribe → paste)")
    print("   Press Ctrl+C to quit.\n")
    
    start_websocket_thread()
    
    def sigint_handler(signum, frame):
        print("\n⏹️ Shutting down...")
        sys.exit(0)
    signal.signal(signal.SIGINT, sigint_handler)
    
    # Start warmup via worker
    mlx_request_queue.put({"type": "warmup"})
    
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                        dtype="int16", callback=audio_callback):
        with keyboard.Listener(on_press=on_press) as listener:
            listener.join()

if __name__ == "__main__":
    main()
