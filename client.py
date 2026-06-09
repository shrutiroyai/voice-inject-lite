#!/usr/bin/env python3
"""Voice Inject Client — cross-platform voice-to-text with auto-paste."""

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

import platform_support
from platform_support import _system_awake, copy_and_paste, get_platform_name

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
_selected_text_buffer = ""    # Captured text from selection
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

if platform_support.PLATFORM == "darwin":
    _MLX_MODEL = "mlx-community/whisper-large-v3-mlx"
    _LLM_MODEL = "mlx-community/Phi-3.5-mini-instruct-4bit"
else:
    _MLX_MODEL = "openai/whisper-large-v3"
    _LLM_MODEL = "microsoft/Phi-3.5-mini-instruct"

_WHISPER_HALLUCINATIONS = {
    "thank you", "thanks for watching", "thank you for watching",
    "subscribe", "like and subscribe", "bye", "the end",
    "more paste", "subtitle by", "subtitles by", "transcribed by",
    "please subscribe", "have a great day", "thank you very much"
}

import re

def dedup_repetitions(text):
    """Detect and remove phrase-level repetition loops from Whisper output."""
    if not text:
        return text
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    if len(sentences) < 3:
        return text

    # Count occurrences (case-insensitive)
    seen = {}
    for s in sentences:
        key = s.lower().strip().rstrip('.')
        seen[key] = seen.get(key, 0) + 1

    # If any phrase repeats 3+ times, it's a loop
    has_loop = any(count >= 3 for count in seen.values())
    if not has_loop:
        return text

    # Keep only the first occurrence of each sentence
    result = []
    added = set()
    for s in sentences:
        k = s.lower().strip().rstrip('.')
        if k not in added:
            result.append(s)
            added.add(k)

    # If after dedup we only have repetitive fragments, keep just the first
    if len(result) <= 2:
        return result[0] if result else text
    return " ".join(result)

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
def play_cue(frequency=800, duration=0.08):
    """Play a soft water-drop sound (quick descending pitch with decay)."""
    try:
        n = int(SAMPLE_RATE * duration)
        t = np.linspace(0, duration, n, False)
        freq_sweep = frequency * np.exp(-12 * t)
        phase = 2 * np.pi * np.cumsum(freq_sweep) / SAMPLE_RATE
        decay = np.exp(-30 * t)
        wave = 0.04 * decay * np.sin(phase)
        sd.play(wave, SAMPLE_RATE)
    except:
        pass

# === COMMAND DETECTION ===

_COMMAND_TRIGGERS = [
    "rewrite this", "rewrite it", "rephrase this", "rephrase it",
    "make it", "make this", "say it", "say this",
    "change the tone", "change it to", "change this to",
    "more formal", "more friendly", "more professional", "more casual",
    "more concise", "more polite", "more direct",
    "ensure clarity", "with clarity", "sound professional",
    "sound friendly", "sound formal", "sound casual",
    "write it as", "put it as", "phrase it as",
]

def detect_command(text):
    """Detect if transcription contains a rewrite/style command.
    Returns the command trigger found, or None if plain transcription."""
    text_lower = text.lower()
    for trigger in _COMMAND_TRIGGERS:
        if trigger in text_lower:
            return trigger
    return None

def build_command_prompt(raw_text, prev_context):
    """Build a prompt that executes a rewrite command on text."""
    context_block = ""
    if prev_context:
        context_block = f"\nThe user's previous text (which \"this\" or \"it\" may refer to): {prev_context}"

    return f"""<|system|>
You are a writing assistant. The user will give you an instruction to rewrite or restyle some text.
- If the instruction says "this" or "it", apply it to their previous text shown below.
- If the instruction contains both content and a command (e.g., "tell the team I'm leaving, make it friendly"), separate them: apply the command to the content.
- Output ONLY the rewritten text. No explanations, no preamble.
- English only.{context_block}<|end|>
<|user|>
{raw_text}<|end|>
<|assistant|>
"""

# === MLX WORKER THREAD ===

mlx_request_queue = queue.Queue()
_llm_model = None
_llm_tokenizer = None

def _init_inference_backend():
    """Initialize the appropriate ML backend for this platform. Returns (transcribe_fn, load_llm_fn, generate_fn)."""
    if platform_support.PLATFORM == "darwin":
        import mlx.core as mx
        import mlx_whisper
        from mlx_lm import load, generate
        mx.set_default_device(mx.gpu)

        def transcribe_fn(audio, initial_prompt):
            return mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=_MLX_MODEL,
                language="en",
                condition_on_previous_text=False,
                initial_prompt=initial_prompt,
                no_speech_threshold=0.3,
                logprob_threshold=-0.8,
                temperature=0.0
            )

        def warmup_whisper():
            silence = np.zeros(16000, dtype=np.float32)
            mlx_whisper.transcribe(silence, path_or_hf_repo=_MLX_MODEL, condition_on_previous_text=False)

        return transcribe_fn, warmup_whisper, load, generate
    else:
        import torch
        from transformers import pipeline as hf_pipeline, AutoModelForCausalLM, AutoTokenizer

        _whisper_pipe = [None]

        def transcribe_fn(audio, initial_prompt):
            if _whisper_pipe[0] is None:
                device = "cuda" if torch.cuda.is_available() else "cpu"
                _whisper_pipe[0] = hf_pipeline(
                    "automatic-speech-recognition", model=_MLX_MODEL,
                    device=device, torch_dtype=torch.float16 if device == "cuda" else torch.float32
                )
            result = _whisper_pipe[0](
                audio,
                generate_kwargs={"language": "en", "initial_prompt": initial_prompt},
                return_timestamps=True
            )
            text = result.get("text", "").strip()
            chunks = result.get("chunks", [])
            segments = [{"text": c.get("text", ""), "avg_logprob": -0.3, "no_speech_prob": 0.0} for c in chunks] if chunks else [{"text": text, "avg_logprob": -0.3}]
            return {"text": text, "segments": segments}

        def warmup_whisper():
            silence = np.zeros(16000, dtype=np.float32)
            transcribe_fn(silence, None)

        def load_llm(model_name):
            device = "cuda" if torch.cuda.is_available() else "cpu"
            tokenizer = AutoTokenizer.from_pretrained(model_name)
            model = AutoModelForCausalLM.from_pretrained(
                model_name, torch_dtype=torch.float16 if device == "cuda" else torch.float32
            ).to(device)
            return model, tokenizer

        def generate_llm(model, tokenizer, prompt="", max_tokens=150):
            device = next(model.parameters()).device
            inputs = tokenizer(prompt, return_tensors="pt").to(device)
            with torch.no_grad():
                outputs = model.generate(**inputs, max_new_tokens=max_tokens, do_sample=False)
            new_tokens = outputs[0][inputs["input_ids"].shape[1]:]
            return tokenizer.decode(new_tokens, skip_special_tokens=True)

        return transcribe_fn, warmup_whisper, load_llm, generate_llm


def mlx_worker():
    """Dedicated thread for all Whisper and LLM operations."""
    global _llm_model, _llm_tokenizer, _warmup_done

    transcribe_fn, warmup_whisper, load, generate = _init_inference_backend()

    while True:
        request = mlx_request_queue.get()
        if request is None: break

        req_type = request.get("type")
        callback = request.get("callback")

        try:
            hf_token = get_config_setting("huggingface_token", "").strip()
            if hf_token:
                os.environ["HF_TOKEN"] = hf_token

            if req_type == "warmup":
                print("⏳ Warming up models...")
                message_queue.put({"type": "warmup_progress", "percent": 10, "message": "Starting warmup..."})

                message_queue.put({"type": "warmup_progress", "percent": 20, "message": "Loading Whisper..."})
                try:
                    warmup_whisper()
                    print("✅ Whisper warm")
                except Exception as e:
                    print(f"⚠️ Whisper load failed: {e}")
                    message_queue.put({"type": "warmup_progress", "percent": 20, "message": f"Whisper Error: {str(e)[:50]}"})
                    time.sleep(2)

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

                prompt_parts = []
                if vocab:
                    prompt_parts.append(vocab)
                if _recent_context:
                    context_str = " ".join(text for text, _ in _recent_context)
                    prompt_parts.append(context_str)
                initial_prompt = ". ".join(prompt_parts) if prompt_parts else None

                result = transcribe_fn(audio, initial_prompt)

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

                filtered_text = dedup_repetitions(filtered_text)

                if is_hallucination(filtered_text):
                    filtered_text = ""

                if filtered_text and avg_confidence > _MIN_CONTEXT_CONFIDENCE:
                    _recent_context.append((filtered_text, avg_confidence))
                    while len(_recent_context) > _MAX_CONTEXT_ITEMS:
                        _recent_context.pop(0)

                if callback: callback(filtered_text)

            elif req_type == "cleanup":
                global _selected_text_buffer
                raw_text = request.get("text")
                if not raw_text.strip():
                    if callback: callback(raw_text)
                    continue

                if _llm_model is None:
                    _llm_model, _llm_tokenizer = load(_LLM_MODEL)

                last_transcription = _recent_context[-1][0] if _recent_context else ""
                older_context = " ".join(text for text, _ in _recent_context[:-1]) if len(_recent_context) > 1 else ""

                if _selected_text_buffer:
                    # STRICT COMMAND MODE: use selection as content, voice as instruction
                    prompt = f"""<|system|>
You are a writing assistant. Apply the user's instruction to the provided content.
- Output ONLY the modified text. No explanations, no preamble.
- English only.<|end|>
<|user|>
Instruction: {raw_text}
Content:
{_selected_text_buffer}<|end|>
<|assistant|>
"""
                    _selected_text_buffer = "" # Clear after use
                    response = generate(_llm_model, _llm_tokenizer, prompt=prompt, max_tokens=1000)

                    if "<|end|>" in response:
                        response = response.split("<|end|>")[0]
                    cleaned = response.strip().strip('"').strip("'")
                    if callback: callback(cleaned)

                else:
                    command_mode = detect_command(raw_text)

                    if command_mode:
                    prompt = build_command_prompt(raw_text, last_transcription)
                    response = generate(_llm_model, _llm_tokenizer, prompt=prompt, max_tokens=300)

                    if "<|end|>" in response:
                        response = response.split("<|end|>")[0]
                    for stop in ["\nNote:", "\n---", "\n\n\n"]:
                        if stop in response:
                            response = response.split(stop)[0]

                    cleaned = response.strip().strip('"').strip("'")
                    is_bad = (
                        not cleaned
                        or any(ord(c) > 127 for c in cleaned)
                        or cleaned.lower().startswith(("i'm sorry", "as an ai", "i cannot"))
                    )
                    if callback: callback(raw_text if is_bad else cleaned)
                else:
                    context_hint = ""
                    if older_context:
                        context_hint = f"\nThe user was previously talking about: {older_context}\nUse this ONLY to resolve ambiguous words. Do NOT include any of it in your output."

                    prompt = f"""<|system|>
You are a text corrector. You receive raw speech-to-text output and return the SAME text with fixed grammar and punctuation.
You are NOT a chatbot. Do NOT answer questions. Do NOT give advice. Do NOT have a conversation.
Just return the corrected version of whatever text is given. Nothing more.
- English only.
- Fix capitalization, punctuation, and obvious mistranscriptions.
- If a word seems wrong based on context, fix it (e.g., "arts" -> "ads").
- Do NOT add or remove words beyond minimal fixes.{context_hint}<|end|>
<|user|>
Correct this transcription:
{raw_text}<|end|>
<|assistant|>
"""
                    response = generate(_llm_model, _llm_tokenizer, prompt=prompt, max_tokens=150)

                    if "<|end|>" in response:
                        response = response.split("<|end|>")[0]
                    for stop in ["\n(", "\n\n", "\nNote:", "\n---", "\nI ", "\nAs ", "\nYes", "\nSure"]:
                        if stop in response:
                            response = response.split(stop)[0]

                    cleaned = response.strip()

                    input_words = set(raw_text.lower().split())
                    output_words = set(cleaned.lower().split())
                    overlap = len(input_words & output_words) / max(len(input_words), 1)

                    is_bad = (
                        not cleaned
                        or len(cleaned) > len(raw_text) * 3
                        or any(ord(c) > 127 for c in cleaned)
                        or cleaned.lower().startswith(("i'm sorry", "as an ai", "i cannot", "yes,", "sure,", "of course"))
                        or overlap < 0.4
                    )
                    if callback: callback(raw_text if is_bad else cleaned)

        except Exception as e:
            print(f"⚠️ Worker Error ({req_type}): {e}")
            if callback: callback(None)

        mlx_request_queue.task_done()

# Start worker thread
threading.Thread(target=mlx_worker, daemon=True).start()

_raw_buffer = []  # Pre-gain buffer for VAD checks

def audio_callback(indata, frames, time_info, status):
    global _raw_buffer
    if command_recording:
        _raw_buffer.append(indata.copy())
        if _input_gain != 1.0:
            amplified = np.clip(indata.astype(np.float32) * _input_gain, -32768, 32767).astype(np.int16)
            command_buffer.append(amplified)
        else:
            command_buffer.append(indata.copy())

def paste_text(text: str):
    """Copy to clipboard and auto-paste. Platform-aware."""
    copy_and_paste(text)

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
    global command_buffer, _raw_buffer
    import webrtcvad
    vad = webrtcvad.Vad(3)
    frame_duration_ms = 30
    frame_size = int(SAMPLE_RATE * frame_duration_ms / 1000)
    silence_threshold_frames = int(0.3 * 1000 / frame_duration_ms)
    silence_frames = 0
    has_speech = False

    while command_recording:
        time.sleep(0.1)
        if not _raw_buffer:
            continue

        # Use pre-gain audio for VAD so amplified noise doesn't fool it
        raw_audio = np.concatenate(_raw_buffer, axis=0).flatten()
        if len(raw_audio) < frame_size:
            continue

        last_frame = raw_audio[-frame_size:]
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
            _raw_buffer = []
            silence_frames = 0
            has_speech = False

            audio_data = np.concatenate(captured, axis=0)

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

_MAX_WHISPER_SECONDS = 20  # Cap audio chunks to prevent hallucination loops

def command_flush_remaining():
    global command_buffer, _raw_buffer
    captured, command_buffer = command_buffer, []
    _raw_buffer = []
    if not captured:
        return

    audio_data = np.concatenate(captured, axis=0)

    # Cap length to prevent Whisper from hallucinating on long noisy buffers
    max_samples = SAMPLE_RATE * _MAX_WHISPER_SECONDS
    if len(audio_data) > max_samples:
        audio_data = audio_data[:max_samples]

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
    global command_recording, command_buffer, _command_cooldown, _last_recording_end, _selected_text_buffer
    now = time.time()
    if now - _command_cooldown < 1.0:
        return
    if not command_recording:
        # Capture selection before starting
        _selected_text_buffer = platform_support.get_selected_text()
        if _selected_text_buffer:
            print(f"📋 Selection captured: {len(_selected_text_buffer)} chars")

        # Reset context if it's been a while since last session
        if _recent_context and (now - _last_recording_end) > _CONTEXT_RESET_SECONDS:
            _recent_context.clear()
        command_recording = True
        command_buffer = []
        _raw_buffer.clear()
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
    if key == platform_support.get_hotkey_key():
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
    """Maintain audio stream, reconnecting on device changes or wake from sleep."""
    while True:
        _system_awake.wait()
        try:
            calibrate_input_gain()
            initial_device = sd.default.device[0]
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                                dtype="int16", callback=audio_callback):
                while True:
                    time.sleep(1)
                    if not _system_awake.is_set():
                        print("💤 Sleep detected, releasing audio device...")
                        break
                    if sd.default.device[0] != initial_device:
                        print("🔄 Audio input device changed, reconnecting...")
                        break
        except Exception as e:
            print(f"🔄 Audio device error, reconnecting... ({e})")
            time.sleep(1)



def keyboard_listener_loop():
    """Run keyboard listener, restarting after sleep/wake cycles."""
    while True:
        _system_awake.wait()
        try:
            with keyboard.Listener(on_press=on_press) as listener:
                while listener.running:
                    if not _system_awake.is_set():
                        listener.stop()
                        break
                    time.sleep(0.5)
        except Exception as e:
            print(f"⌨️ Keyboard listener restarting... ({e})")
            time.sleep(1)


def main():
    platform_name = get_platform_name()
    hotkey_desc = platform_support.get_hotkey_description()
    print(f"🎙️ Voice Inject ({platform_name})")
    print(f"   {hotkey_desc} (transcribe → paste)")
    print("   Press Ctrl+C to quit.\n")

    start_websocket_thread()

    def sigint_handler(signum, frame):
        print("\n⏹️ Shutting down...")
        sys.exit(0)
    signal.signal(signal.SIGINT, sigint_handler)

    # Start warmup via worker
    mlx_request_queue.put({"type": "warmup"})

    # Sleep/wake observer — platform-detected (macOS/Windows/Linux)
    platform_support.start_sleep_wake_observer()

    # Audio stream in its own thread — reconnects on device changes or wake
    threading.Thread(target=audio_stream_loop, daemon=True).start()

    # Keyboard listener — restarts after sleep/wake
    keyboard_listener_loop()

if __name__ == "__main__":
    main()
