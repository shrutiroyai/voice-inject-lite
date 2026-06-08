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
_input_gain = 1.0             # Auto-gain factor for quiet mics
_recent_context = []          # Rolling context: list of (text, confidence) tuples
_MAX_CONTEXT_ITEMS = 3
_MIN_CONTEXT_CONFIDENCE = -0.4  # avg_logprob threshold

# Message queue for WebSocket
message_queue = queue.Queue()
ws_connected = False
_warmup_done = False

_MLX_MODEL = "mlx-community/whisper-large-v3-mlx"
_LLM_MODEL = "mlx-community/Phi-3.5-mini-instruct-4bit"

_WHISPER_HALLUCINATIONS = {
    "thank you", "thanks for watching", "thank you for watching",
    "subscribe", "like and subscribe", "bye", "the end",
    "more paste", "subtitle by", "subtitles by", "transcribed by",
    "please subscribe", "have a great day", "thank you very much"
}

import re

def is_hallucination(text):
    """Check if the text is likely a Whisper hallucination, including prompt recitation."""
    if not text:
        return True
    
    text_lower = text.lower().strip()
    
    # Remove common punctuation for the check
    clean_text = re.sub(r'[.,!?;:]', '', text_lower).strip()
    if not clean_text:
        return True
        
    # Check if it's just a single character or very common short hallucination word
    if clean_text in ["t", "h", "you", "thanks", "thank"]:
        return True

    # 1. Check against the fixed hallucination list
    sorted_hallucinations = sorted([h.lower() for h in _WHISPER_HALLUCINATIONS], key=len, reverse=True)
    
    remaining_text = clean_text
    while remaining_text:
        match_found = False
        for h in sorted_hallucinations:
            if remaining_text.startswith(h):
                remaining_text = remaining_text[len(h):].strip()
                match_found = True
                break
        if not match_found:
            break
            
    if not remaining_text:
        return True
        
    # 2. Vocabulary Prompt Recitation Check
    # If the transcription consists ONLY of words from your custom vocabulary
    # and nothing else, it's almost certainly a hallucination of the prompt.
    words = clean_text.split()
    vocab_str = get_vocabulary() or ""
    vocab_words = [v.strip().lower() for v in vocab_str.split(",")]
    
    if all(word in vocab_words for word in words):
        # If it's more than one vocab word and nothing else, block it.
        # (Real speech usually has at least one non-vocab word or is a single word)
        if len(words) > 1:
            return True
        
    return False

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
    """Load custom vocabulary from ~/.voice-inject/vocabulary.json and format for Whisper.
    Provides a clean list of words to the prompt to avoid confusing the model with phonetic hints.
    """
    vocab_path = Path.home() / ".voice-inject" / "vocabulary.json"
    if vocab_path.exists():
        try:
            with open(vocab_path) as f:
                data = json.load(f)
                entries = data.get("entries", [])
                # Only use the words, not the hints, for the prompt.
                # Whisper handles plain words better in the initial_prompt.
                words = [e.get("word", "").strip() for e in entries if e.get("word")]
                return ", ".join(words) if words else None
        except Exception:
            pass
    return None

def get_snippets():
    """Load snippets from ~/.voice-inject/snippets.json."""
    snippets_path = Path.home() / ".voice-inject" / "snippets.json"
    if snippets_path.exists():
        try:
            with open(snippets_path) as f:
                data = json.load(f)
                return data.get("entries", [])
        except Exception:
            pass
    return []

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

                # Build initial_prompt: vocabulary + rolling context
                prompt_parts = []
                if vocab:
                    prompt_parts.append(vocab)
                if _recent_context:
                    context_str = " ".join(text for text, _ in _recent_context)
                    prompt_parts.append(context_str)
                initial_prompt = ". ".join(prompt_parts) if prompt_parts else None

                result = mlx_whisper.transcribe(
                    audio,
                    path_or_hf_repo=_MLX_MODEL,
                    language="en",
                    condition_on_previous_text=False,
                    initial_prompt=initial_prompt,
                    no_speech_threshold=0.3,
                    logprob_threshold=-0.8,
                    temperature=0.0
                )

                segments = result.get("segments", [])
                filtered_text = ""
                avg_confidence = 0.0

                if len(segments) > 1:
                    first_seg = segments[0]
                    first_text = (first_seg.get("text") or "").strip().lower()

                    vocab_str = vocab or ""
                    vocab_words = [v.strip().lower() for v in vocab_str.split(",")]

                    is_leading_hallucination = False
                    if first_text in vocab_words:
                        if first_seg.get("no_speech_prob", 0) > 0.2 or first_seg.get("avg_logprob", 0) < -0.5:
                            is_leading_hallucination = True

                    if is_leading_hallucination:
                        used_segments = segments[1:]
                        filtered_text = " ".join([s.get("text", "").strip() for s in used_segments])
                    else:
                        used_segments = segments
                        filtered_text = result.get("text", "").strip()
                    avg_confidence = sum(s.get("avg_logprob", -1) for s in used_segments) / len(used_segments)
                elif segments:
                    filtered_text = result.get("text", "").strip()
                    avg_confidence = segments[0].get("avg_logprob", -1)

                if is_hallucination(filtered_text):
                    filtered_text = ""

                # Update rolling context if confidence is high enough
                if filtered_text and avg_confidence > _MIN_CONTEXT_CONFIDENCE:
                    _recent_context.append((filtered_text, avg_confidence))
                    while len(_recent_context) > _MAX_CONTEXT_ITEMS:
                        _recent_context.pop(0)

                if callback: callback(filtered_text)

            elif req_type == "cleanup":
                raw_text = request.get("text")
                if not raw_text.strip():
                    if callback: callback(raw_text)
                    continue

                if _llm_model is None:
                    _llm_model, _llm_tokenizer = load(_LLM_MODEL)

                context_hint = ""
                if _recent_context:
                    prev = " ".join(text for text, _ in _recent_context[:-1])
                    if prev:
                        context_hint = f"\nThe user was previously talking about: {prev}\nUse this ONLY to resolve ambiguous words. Do NOT include any of it in your output."

                prompt = f"""<|system|>
You fix grammar and punctuation in speech transcriptions. Rules:
- Output ONLY the corrected version of the text given by the user.
- Do NOT output anything else. No notes, no context, no preamble.
- Do NOT change tone or remove filler words.
- If a word seems wrong based on topic context, fix it (e.g., "arts" -> "ads" if topic is advertising).{context_hint}<|end|>
<|user|>
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
        if _input_gain != 1.0:
            amplified = np.clip(indata.astype(np.float32) * _input_gain, -32768, 32767).astype(np.int16)
            command_buffer.append(amplified)
        else:
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
                # Apply snippets (case-insensitive, handles possessives)
                import re
                snippets = get_snippets()
                for s in snippets:
                    trigger = s.get("trigger", "").strip()
                    expansion = s.get("text", "").strip()
                    if trigger and expansion:
                        # Create a flexible regex:
                        # 1. Split trigger into words
                        # 2. Allow each word to have an optional 's or s
                        # 3. Allow flexible whitespace between words
                        parts = trigger.split()
                        pattern_str = r"\s+".join([re.escape(p) + r"('?s)?" for p in parts])
                        pattern = re.compile(pattern_str, re.IGNORECASE)
                        cleaned = pattern.sub(expansion, cleaned)
                
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
            
            # 1. Ignore very short audio (less than 300ms) - likely noise/clicks
            if len(audio_data) < SAMPLE_RATE * 0.3:
                continue

            rms = np.sqrt(np.mean(audio_data.astype(np.float64) ** 2))
            
            # Re-read config for real-time UI updates
            min_energy = int(get_config_setting("min_speech_energy", "250"))
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
    min_energy = int(get_config_setting("min_speech_energy", "250"))
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
_last_recording_end = 0
_CONTEXT_RESET_SECONDS = 30

def toggle_command():
    global command_recording, command_buffer, _command_cooldown, _last_recording_end
    now = time.time()
    if now - _command_cooldown < 1.0:
        return
    if not command_recording:
        # Reset context if it's been a while since last session
        if _recent_context and (now - _last_recording_end) > _CONTEXT_RESET_SECONDS:
            _recent_context.clear()
        command_recording = True
        command_buffer = []
        play_cue(frequency=1000) # High blip for START
        print("🎤 Command mode: recording...")
        message_queue.put({"type": "status", "recording": True, "mode": "command"})
        threading.Thread(target=command_vad_loop, daemon=True).start()
    else:
        command_recording = False
        _command_cooldown = now
        _last_recording_end = now
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

def calibrate_input_gain():
    """Set gain from config, or auto-detect if not configured."""
    global _input_gain
    configured_gain = float(get_config_setting("input_gain", "0"))
    if configured_gain > 0:
        _input_gain = configured_gain
        if configured_gain != 1.0:
            print(f"🔊 Using configured gain: {_input_gain:.1f}x")
        return
    try:
        samples = []
        def cal_cb(indata, frames, time_info, status):
            samples.append(indata.copy())
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                            dtype="int16", callback=cal_cb):
            time.sleep(0.5)
        if samples:
            audio = np.concatenate(samples).flatten()
            peak = float(np.abs(audio).max())
            if peak < 500:
                _input_gain = min(32768.0 / max(peak * 4, 1), 40.0)
                print(f"🔊 Low mic detected (peak={peak:.0f}), applying {_input_gain:.1f}x gain")
            else:
                _input_gain = 1.0
    except Exception:
        _input_gain = 1.0

def audio_stream_loop():
    """Maintain audio stream, reconnecting on device changes."""
    while True:
        try:
            calibrate_input_gain()
            initial_device = sd.default.device[0]
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                                dtype="int16", callback=audio_callback):
                while True:
                    time.sleep(1)
                    if sd.default.device[0] != initial_device:
                        print("🔄 Audio input device changed, reconnecting...")
                        break
        except Exception as e:
            print(f"🔄 Audio device changed, reconnecting... ({e})")
            time.sleep(1)

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

    # Audio stream in its own thread — reconnects on device changes
    threading.Thread(target=audio_stream_loop, daemon=True).start()

    # Keyboard listener runs independently
    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()

if __name__ == "__main__":
    main()
