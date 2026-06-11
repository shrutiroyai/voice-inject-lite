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
_selected_text_buffer = ""    # Captured text from selection
last_option_press = 0
DOUBLE_TAP_THRESHOLD = 0.6
_input_gain = 1.0             # Auto-gain factor for quiet mics
_recent_context = []          # Rolling context: list of (text, confidence) tuples
_MAX_CONTEXT_ITEMS = 3
_MIN_CONTEXT_CONFIDENCE = -0.4  # avg_logprob threshold

class AudioRecorder:
    def __init__(self):
        self.is_recording = False
        self.lock = threading.Lock()
        self.max_samples = SAMPLE_RATE * 600  # 10 minutes
        self.buffer = np.zeros(self.max_samples, dtype=np.int16)
        self.write_pos = 0
        self.total_samples = 0
        self.session_start_samples = 0

    def start(self):
        with self.lock:
            self.session_start_samples = self.total_samples
            self.is_recording = True

    def stop(self):
        with self.lock:
            self.is_recording = False
            start_monotonic = self.session_start_samples
            end_monotonic = self.total_samples
            
            num_samples = end_monotonic - start_monotonic
            if num_samples <= 0:
                return []
            
            if num_samples > self.max_samples:
                num_samples = self.max_samples
                start_monotonic = end_monotonic - num_samples
                
            start_idx = int(start_monotonic % self.max_samples)
            end_idx = int(end_monotonic % self.max_samples)
            
            if start_idx < end_idx:
                retrieved = self.buffer[start_idx:end_idx].copy()
            else:
                retrieved = np.concatenate([self.buffer[start_idx:], self.buffer[:end_idx]])
            
            # Apply input gain only when pulling audio
            if _input_gain != 1.0:
                retrieved = np.clip(retrieved.astype(np.float32) * _input_gain, -32768, 32767).astype(np.int16)
            
            return [retrieved]

    def clear(self):
        with self.lock:
            self.is_recording = False

    def add_chunk(self, indata):
        """Always record into the circular buffer, regardless of is_recording."""
        with self.lock:
            data = indata.flatten()
            n = len(data)
            
            if n > self.max_samples:
                data = data[-self.max_samples:]
                n = self.max_samples
            
            end_pos = self.write_pos + n
            if end_pos <= self.max_samples:
                self.buffer[self.write_pos:end_pos] = data
            else:
                first_part = self.max_samples - self.write_pos
                self.buffer[self.write_pos:] = data[:first_part]
                self.buffer[:end_pos % self.max_samples] = data[first_part:]
            
            self.write_pos = (self.write_pos + n) % self.max_samples
            self.total_samples += n

    def get_monotonic_range(self, start_monotonic, end_monotonic):
        with self.lock:
            num_samples = end_monotonic - start_monotonic
            if num_samples <= 0:
                return np.array([], dtype=np.int16)
            
            if num_samples > self.max_samples:
                num_samples = self.max_samples
                start_monotonic = end_monotonic - num_samples
                
            start_idx = int(start_monotonic % self.max_samples)
            end_idx = int(end_monotonic % self.max_samples)
            
            if start_idx < end_idx:
                retrieved = self.buffer[start_idx:end_idx].copy()
            else:
                retrieved = np.concatenate([self.buffer[start_idx:], self.buffer[:end_idx]])
            
            # Apply input gain
            if _input_gain != 1.0:
                retrieved = np.clip(retrieved.astype(np.float32) * _input_gain, -32768, 32767).astype(np.int16)
            
            return retrieved

    def get_raw_tail(self, frames):
        """Returns the last N frames from the circular buffer as a flat numpy array."""
        with self.lock:
            if frames <= 0 or self.total_samples == 0:
                return np.array([], dtype=np.int16)
            
            n = min(frames, self.total_samples, self.max_samples)
            
            end_monotonic = self.total_samples
            start_monotonic = end_monotonic - n
            
            start_idx = int(start_monotonic % self.max_samples)
            end_idx = int(end_monotonic % self.max_samples)
            
            if start_idx < end_idx:
                return self.buffer[start_idx:end_idx].copy()
            else:
                return np.concatenate([self.buffer[start_idx:], self.buffer[:end_idx]])

recorder = AudioRecorder()

# Message queue for WebSocket
message_queue = queue.Queue()
ws_connected = False
_warmup_done = False

if platform_support.PLATFORM == "darwin":
    _MLX_MODEL = "mlx-community/whisper-large-v3-turbo"
    _LLM_MODEL = "mlx-community/Qwen2.5-1.5B-Instruct-4bit"
    _REWRITE_MODEL = "mlx-community/Phi-3.5-mini-instruct-4bit"
else:
    _MLX_MODEL = "openai/whisper-turbo"
    _LLM_MODEL = "microsoft/Phi-3.5-mini-instruct"
    _REWRITE_MODEL = "microsoft/Phi-3.5-mini-instruct"

_WHISPER_HALLUCINATIONS = {
    "thank you", "thanks for watching", "thank you for watching",
    "subscribe", "like and subscribe", "bye", "the end",
    "more paste", "subtitle by", "subtitles by", "transcribed by",
    "please subscribe", "have a great day", "thank you very much",
    "amara.org", "amara org", "amara community", "community subtitles",
    "subtitle community", "captioned by", "captions by", "english subtitles",
    "translated by", "all rights reserved", "unauthorized use prohibited",
    "i'm not sure if i'm doing this right", "i'm not sure if i'm doing this right or wrong",
    "is this working", "can you hear me", "testing one two three"
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
    
    # 1. Check against the fixed hallucination list (fuzzy matching)
    # If the text contains any of these phrases or consists ONLY of them
    for h in _WHISPER_HALLUCINATIONS:
        if h in text_lower:
            # If it's a very short text containing a hallucination phrase, block it
            if len(text_lower) < len(h) + 10:
                return True
    
    # Remove common punctuation for further checks
    clean_text = re.sub(r'[.,!?;:]', '', text_lower).strip()
    if not clean_text:
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

# === MLX WORKER THREAD ===

mlx_request_queue = queue.Queue()
_llm_model = None
_llm_tokenizer = None
_rewrite_model = None
_rewrite_tokenizer = None

def _init_inference_backend():
    """Initialize the appropriate ML backend for this platform. Returns (transcribe_fn, load_llm_fn, generate_fn)."""
    if platform_support.PLATFORM == "darwin":
        import mlx.core as mx
        import mlx_whisper
        from mlx_lm import load, generate as mlx_generate
        from mlx_lm.sample_utils import make_sampler
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

        def generate_fn(model, tokenizer, prompt, max_tokens=100, temp=0.0):
            print(f"DEBUG: Generating with max_tokens={max_tokens}, temp={temp}")
            try:
                # In newer mlx_lm, temp is handled via a sampler
                sampler = make_sampler(temp=temp)
                res = mlx_generate(model, tokenizer, prompt=prompt, max_tokens=max_tokens, sampler=sampler)
                print("DEBUG: Generation complete")
                return res
            except Exception as e:
                print(f"DEBUG: Generation failed: {e}")
                raise e

        return transcribe_fn, warmup_whisper, load, generate_fn
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

        def generate_llm(model, tokenizer, prompt="", max_tokens=150, temp=0.0):
            device = next(model.parameters()).device
            inputs = tokenizer(prompt, return_tensors="pt").to(device)
            with torch.no_grad():
                # For transformers, temp > 0 requires do_sample=True
                do_sample = temp > 0
                outputs = model.generate(
                    **inputs, 
                    max_new_tokens=max_tokens, 
                    do_sample=do_sample,
                    temperature=temp if do_sample else 1.0
                )
            new_tokens = outputs[0][inputs["input_ids"].shape[1]:]
            return tokenizer.decode(new_tokens, skip_special_tokens=True)

        return transcribe_fn, warmup_whisper, load_llm, generate_llm


def mlx_worker():
    """Dedicated thread for all Whisper and LLM operations."""
    global _llm_model, _llm_tokenizer, _rewrite_model, _rewrite_tokenizer, _warmup_done

    transcribe_fn, warmup_whisper, load, generate = _init_inference_backend()

    while True:
        request = mlx_request_queue.get()
        if request is None: break

        req_type = request.get("type")
        callback = request.get("callback")

        try:
            hf_token = get_config_setting("huggingface_token", "").strip()
            if hf_token and hf_token.startswith("hf_"):
                os.environ["HF_TOKEN"] = hf_token
            elif "HF_TOKEN" in os.environ:
                del os.environ["HF_TOKEN"]

            if req_type == "warmup":
                print("⏳ Warming up models...")
                message_queue.put({"type": "warmup_progress", "percent": 10, "message": "Starting warmup..."})

                # Load LLM first
                message_queue.put({"type": "warmup_progress", "percent": 20, "message": "Loading LLM..."})
                if _llm_model is None:
                    try:
                        print(f"⏳ Loading LLM: {_LLM_MODEL}")
                        _llm_model, _llm_tokenizer = load(_LLM_MODEL)
                        print("✅ LLM loaded")
                    except Exception as e:
                        print(f"⚠️ LLM load failed: {e}")
                        message_queue.put({"type": "warmup_progress", "percent": 20, "message": f"LLM Error: {str(e)[:50]}"})
                        time.sleep(2)

                # Then Whisper
                message_queue.put({"type": "warmup_progress", "percent": 50, "message": "LLM ready. Loading Whisper..."})
                try:
                    print(f"⏳ Loading Whisper: {_MLX_MODEL}")
                    warmup_whisper()
                    print("✅ Whisper loaded")
                except Exception as e:
                    print(f"⚠️ Whisper load failed: {e}")
                    message_queue.put({"type": "warmup_progress", "percent": 50, "message": f"Whisper Error: {str(e)[:50]}"})
                    time.sleep(2)

                message_queue.put({"type": "warmup_progress", "percent": 90, "message": "Models ready. Finalizing..."})

                _warmup_done = True
                message_queue.put({"type": "warmup_complete"})
                print("🔥 Models ready\n")

            elif req_type == "transcribe":
                audio = request.get("audio")
                vocab = get_vocabulary()

                prompt_parts = []
                # Simple style hint - Whisper uses this as a phonetic/punctuation guide
                prompt_parts.append("Hello! How are you?")
                if vocab:
                    prompt_parts.append(vocab)
                if _recent_context:
                    context_str = " ".join(text for text, _ in _recent_context)
                    prompt_parts.append(context_str)
                initial_prompt = ". ".join(prompt_parts) if prompt_parts else None

                print(f"🎙️ Transcribing ({len(audio)/SAMPLE_RATE:.1f}s)...")
                result = transcribe_fn(audio, initial_prompt)
                print(f"📄 Raw Transcription ({request.get('source', 'Unknown')}): {result.get('text', '').strip()}")

                segments = result.get("segments", [])
                
                # Industry-standard segment-level filtering
                # Filter out segments that are likely silence or noise (high no_speech_prob)
                # or very low confidence (low avg_logprob)
                valid_segments = []
                for s in segments:
                    no_speech = s.get("no_speech_prob", 0)
                    confidence = s.get("avg_logprob", -1)
                    # Tighten thresholds: Turbo is more 'confident' in hallucinations
                    if no_speech < 0.40 and confidence > -0.6:
                        valid_segments.append(s)
                
                filtered_text = " ".join([s.get("text", "").strip() for s in valid_segments])
                
                # Re-calculate confidence based on valid segments
                if valid_segments:
                    avg_confidence = sum(s.get("avg_logprob", -1) for s in valid_segments) / len(valid_segments)
                else:
                    avg_confidence = -1.0

                filtered_text = dedup_repetitions(filtered_text)

                if is_hallucination(filtered_text):
                    print(f"🚫 Hallucination blocked: {filtered_text}")
                    filtered_text = ""
                else:
                    if filtered_text:
                        print(f"✅ Filtered Result: {filtered_text}")

                if filtered_text and avg_confidence > _MIN_CONTEXT_CONFIDENCE and request.get("source") != "Partial":
                    _recent_context.append((filtered_text, avg_confidence))
                    while len(_recent_context) > _MAX_CONTEXT_ITEMS:
                        _recent_context.pop(0)

                if callback: 
                    # If it's the standard transcription callback, pass the source
                    import inspect
                    sig = inspect.signature(callback)
                    if "source" in sig.parameters:
                        callback(filtered_text, source=request.get("source", "Unknown"))
                    else:
                        callback(filtered_text)

            elif req_type == "cleanup":
                raw_text = request.get("text")
                selection = request.get("selection", "")
                print(f"✨ Cleanup requested (selection={len(selection)} chars)")
                
                if not raw_text.strip():
                    if callback: callback(raw_text)
                    continue

                if _llm_model is None:
                    _llm_model, _llm_tokenizer = load(_LLM_MODEL)

                last_transcription = _recent_context[-1][0] if _recent_context else ""
                older_context = " ".join(text for text, _ in _recent_context[:-1]) if len(_recent_context) > 1 else ""

                if selection:
                    # STRICT COMMAND MODE: use selection as content, voice as instruction
                    # Lazy-load the rewrite model (Phi-3.5-mini) for better quality
                    if _rewrite_model is None:
                        print(f"⏳ Loading rewrite model: {_REWRITE_MODEL}")
                        _rewrite_model, _rewrite_tokenizer = load(_REWRITE_MODEL)
                        print("✅ Rewrite model loaded")

                    messages = [
                        {"role": "system", "content": (
                            "You are a precision writing tool. Apply the <instruction> to the <content>.\n\n"
                            "RULES:\n"
                            "1. English ONLY.\n"
                            "2. Fix grammar and punctuation.\n"
                            "3. Maintain the user's original tone.\n"
                            "4. Output ONLY the final text. No explanations, no preamble, no tags."
                        )},
                        {"role": "user", "content": f"<instruction>{raw_text}</instruction>\n<content>\n{selection}\n</content>"}
                    ]
                    prompt = _rewrite_tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)

                    # Dynamic max_tokens: ensure enough room for the rewrite but don't over-generate
                    dynamic_max = min(max(len(selection.split()) * 4, 300), 2000)

                    response = generate(
                        _rewrite_model,
                        _rewrite_tokenizer,
                        prompt=prompt,
                        max_tokens=dynamic_max,
                        temp=0.2
                    )
                    print("✨ LLM response received")

                    # Handle multiple potential end tags and conversational filler
                    for stop_tag in ["<|im_end|>", "<|end|>", "<|endoftext|>", "Note:", "---", "\n("]:
                        if stop_tag in response:
                            response = response.split(stop_tag)[0]

                    cleaned = response.strip().strip('"').strip("'")
                    if callback: callback(cleaned)

                else:
                    history_context = ""
                    if older_context:
                        history_context = f"<history>{older_context}</history>\n"

                    messages = [
                        {"role": "system", "content": (
                            "You are a transcription corrector. Fix grammar, punctuation, and self-corrections.\n\n"
                            "RULES:\n"
                            "1. English ONLY.\n"
                            "2. Resolve self-corrections (e.g., 'let's meet at 2... no 3' becomes 'Let's meet at 3').\n"
                            "3. Preserve ALL details and information. Do not summarize or omit anything.\n"
                            "4. Maintain the user's original tone. Do not make it more formal.\n"
                            "5. Output ONLY the corrected text. No explanations."
                        )},
                        {"role": "user", "content": f"{history_context}Text: {raw_text}"}
                    ]
                    prompt = _llm_tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)

                    # Increased to 1000 to prevent cutoffs in long paragraphs
                    response = generate(_llm_model, _llm_tokenizer, prompt=prompt, max_tokens=1000, temp=0.1)

                    # Handle multiple potential end tags
                    for stop_tag in ["<|im_end|>", "<|end|>", "<|endoftext|>"]:
                        if stop_tag in response:
                            response = response.split(stop_tag)[0]
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

def audio_callback(indata, frames, time_info, status):
    recorder.add_chunk(indata)

def paste_text(text: str):
    """Copy to clipboard and auto-paste. Platform-aware."""
    copy_and_paste(text)

test_mode_active = False

_snippet_cache = None
_snippet_last_load = 0
_SNIPPET_CACHE_TTL = 30  # seconds

def get_cached_snippets():
    """Load and compile snippets with caching."""
    global _snippet_cache, _snippet_last_load
    now = time.time()
    if _snippet_cache is not None and (now - _snippet_last_load) < _SNIPPET_CACHE_TTL:
        return _snippet_cache

    raw_snippets = get_snippets()
    compiled = []
    import re
    for s in raw_snippets:
        trigger = s.get("trigger", "").strip()
        expansion = s.get("text", "").strip()
        if trigger and expansion:
            parts = trigger.split()
            # Flexible regex for plural/possessive
            pattern_str = r"\s+".join([re.escape(p) + r"('?s)?" for p in parts])
            try:
                pattern = re.compile(pattern_str, re.IGNORECASE)
                compiled.append((pattern, expansion))
            except Exception:
                continue
    
    _snippet_cache = compiled
    _snippet_last_load = now
    return compiled

def handle_transcription_result(text: str, source: str = "Unknown"):
    """Callback from worker thread when transcription is done."""
    global _is_processing
    keep_locked = False
    try:
        global test_mode_active, _selected_text_buffer
        if test_mode_active:
            res_text = text if text else "(No speech detected)"
            print(f"🔬 Test Result ({source}): {res_text}")
            message_queue.put({"type": "test_mic_result", "text": res_text})
            test_mode_active = False
            return

        if text:
            # Capture current selection and clear global buffer for this session
            sel = _selected_text_buffer
            _selected_text_buffer = ""

            def handle_cleanup_result(cleaned: str):
                global _is_processing
                try:
                    if cleaned:
                        # Apply snippets
                        snippets = get_cached_snippets()
                        for pattern, expansion in snippets:
                            cleaned = pattern.sub(expansion, cleaned)
                        
                        print(f"✨ {cleaned}")
                        paste_text(cleaned)
                finally:
                    _is_processing = False
            
            if sel:
                # Selection Command Mode: Use LLM
                print(f"🧠 Processing rewrite for {len(sel)} chars...")
                keep_locked = True
                mlx_request_queue.put({
                    "type": "cleanup",
                    "text": text,
                    "selection": sel,
                    "callback": handle_cleanup_result
                })
            else:
                # Standard Dictation: Use LLM for cleanup/punctuation with high speed
                print(f"✨ Cleaning up transcription...")
                keep_locked = True
                mlx_request_queue.put({
                    "type": "cleanup",
                    "text": text,
                    "selection": "",
                    "callback": handle_cleanup_result
                })
        else:
            print("📭 No text transcribed (or filtered out)")
    finally:
        if not keep_locked:
            _is_processing = False

def handle_partial_result(text, source="Partial"):
    """Handle background partial transcriptions."""
    if text:
        print(f"[PARTIAL] {text}")

def command_vad_loop():
    import webrtcvad
    vad = webrtcvad.Vad(3)
    frame_duration_ms = 30
    frame_size = int(SAMPLE_RATE * frame_duration_ms / 1000)
    silence_threshold_frames = int(0.4 * 1000 / frame_duration_ms)
    silence_frames = 0
    has_speech = False
    
    last_partial_trigger_samples = recorder.session_start_samples

    while recorder.is_recording:
        time.sleep(0.1)
        
        # Rolling window peeking: every 10 seconds of recorded audio, trigger a partial transcribe
        total_samples = recorder.total_samples
        if total_samples - last_partial_trigger_samples > SAMPLE_RATE * 10:
            # Extract up to 15 seconds
            window_size = SAMPLE_RATE * 15
            start_m = max(recorder.session_start_samples, total_samples - window_size)
            end_m = total_samples
            
            partial_audio = recorder.get_monotonic_range(start_m, end_m)
            if len(partial_audio) > 0:
                audio_float = partial_audio.astype(np.float32).flatten() / 32768.0
                mlx_request_queue.put({
                    "type": "transcribe",
                    "audio": audio_float,
                    "source": "Partial",
                    "callback": handle_partial_result
                })
            last_partial_trigger_samples = total_samples

        # Check the most recent chunk for ANY speech frames
        # We look at the last 100-200ms and scan for human voice signatures
        recent_audio = recorder.get_raw_tail(frame_size * 2)
        
        if len(recent_audio) < frame_size:
            continue

        # Scan frames in the recent audio for speech
        is_speech = False
        
        # ENERGY GUARDRAIL: If the volume is too low, don't even check the voice signature.
        # This prevents quiet background noise from being mistaken for speech.
        rms = np.sqrt(np.mean(recent_audio.astype(np.float64) ** 2))
        min_energy = int(get_config_setting("min_speech_energy", "250"))
        
        if rms > min_energy * 0.7: # Slightly more sensitive than the final flush check
            for i in range(0, len(recent_audio) - frame_size, frame_size):
                frame = recent_audio[i : i + frame_size]
                fb = frame.astype(np.int16).tobytes()
                try:
                    if vad.is_speech(fb, SAMPLE_RATE):
                        is_speech = True
                        break
                except Exception:
                    continue
        else:
            is_speech = False # Force silence if too quiet

        if is_speech:
            has_speech = True
            silence_frames = 0
        else:
            silence_frames += 1

        # VAD AUTO-TRIGGER DISABLED
        # We no longer auto-transcribe on silence to prevent cutting the user off.
        # Transcription only happens when the user manually stops the recording.
        continue

        if has_speech and silence_frames >= silence_threshold_frames:
            # Check if user manually stopped during this cycle
            if not recorder.is_recording:
                break

            # If we have a selection, don't auto-transcribe partials.
            if _selected_text_buffer:
                continue

            # Lock processing to prevent manual stop from triggering a duplicate
            global _is_processing
            _is_processing = True

            captured = recorder.stop() # This also sets is_recording = False
            # recorder.clear() # Not needed if stop() is enough, but VAD usually stops the session
            silence_frames = 0
            has_speech = False

            if not captured:
                _is_processing = False
                break

            audio_data = np.concatenate(captured, axis=0)

            if len(audio_data) < SAMPLE_RATE * 0.3:
                _is_processing = False
                continue

            rms = np.sqrt(np.mean(audio_data.astype(np.float64) ** 2))
            
            # Re-read config for real-time UI updates
            min_energy = int(get_config_setting("min_speech_energy", "250"))
            if rms < min_energy:
                _is_processing = False
                continue
            
            audio_float = audio_data.astype(np.float32).flatten() / 32768.0
            
            mlx_request_queue.put({
                "type": "transcribe",
                "audio": audio_float,
                "source": "VAD",
                "callback": handle_transcription_result
            })

_MAX_WHISPER_SECONDS = 60  # Allow for long-form dictation up to 1 minute

def command_flush_remaining():
    global _is_processing
    if _is_processing:
        # VAD likely already sent the last chunk, so we just return
        return
    _is_processing = True

    captured = recorder.stop()
    if not captured:
        _is_processing = False
        return

    audio_data = np.concatenate(captured, axis=0)

    # Cap length to prevent Whisper from hallucinating on long noisy buffers
    max_samples = SAMPLE_RATE * _MAX_WHISPER_SECONDS
    if len(audio_data) > max_samples:
        audio_data = audio_data[:max_samples]

    rms = np.sqrt(np.mean(audio_data.astype(np.float64) ** 2))
    min_energy = int(get_config_setting("min_speech_energy", "250"))
    if rms < min_energy:
        _is_processing = False
        return

    audio_float = audio_data.astype(np.float32).flatten() / 32768.0
    mlx_request_queue.put({
        "type": "transcribe",
        "audio": audio_float,
        "source": "Flush",
        "callback": handle_transcription_result
    })
    print("📋 Command mode done.\n")

_command_cooldown = 0
_last_recording_end = 0
_CONTEXT_RESET_SECONDS = 30
_is_processing = False

def toggle_command():
    try:
        global _command_cooldown, _last_recording_end, _selected_text_buffer, _is_processing
        now = time.time()
        
        # Allow STOPPING even if _is_processing is True
        if now - _command_cooldown < 0.5:
            return
        if not recorder.is_recording and _is_processing:
            return

        if not recorder.is_recording:
            # Reset context if it's been a while since last session
            if _recent_context and (now - _last_recording_end) > _CONTEXT_RESET_SECONDS:
                _recent_context.clear()
            
            # Check for selection at the start
            _selected_text_buffer = platform_support.get_selected_text()
            if _selected_text_buffer:
                print(f"📋 Command Mode: Selection captured ({len(_selected_text_buffer)} chars)")
            else:
                print("🎤 Transcription Mode: No selection")

            recorder.start()
            play_cue(frequency=1000) # High blip for START
            print("🎤 Command mode: recording...")
            message_queue.put({"type": "status", "recording": True, "mode": "command"})
            threading.Thread(target=command_vad_loop, daemon=True).start()
        else:
            # Manual STOP
            # Note: recorder.stop() is called inside command_flush_remaining
            # But we set is_recording = False here to stop the VAD loop immediately
            recorder.is_recording = False
            _command_cooldown = now
            _last_recording_end = now
            play_cue(frequency=600) # Low blip for STOP
            print("⏹️ Command mode: stopping...")
            message_queue.put({"type": "status", "recording": False, "mode": "command"})
            threading.Thread(target=command_flush_remaining, daemon=True).start()
    except Exception as e:
        print(f"⚠️ Toggle command error: {e}")
        recorder.clear()
        _is_processing = False

def on_press(key):
    try:
        global last_option_press
        if key == platform_support.get_hotkey_key():
            current_time = time.time()
            if (current_time - last_option_press) < DOUBLE_TAP_THRESHOLD:
                toggle_command()
                last_option_press = 0
            else:
                last_option_press = current_time
    except Exception as e:
        print(f"⚠️ Keyboard handler error: {e}")

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
                    global test_mode_active
                    async for message in websocket:
                        try:
                            msg = json.loads(message)
                            if msg.get("type") == "test_mic_start":
                                test_mode_active = True
                                if not recorder.is_recording:
                                    toggle_command()
                            elif msg.get("type") == "test_mic_stop":
                                if recorder.is_recording:
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
