# 🎙️ Voice Inject Lite

A super-fast, local speech-to-text injector for macOS. Double-tap a hotkey, speak, and have the text auto-pasted directly into any application with perfect grammar and punctuation.

## ✨ Features

- **Double-Tap to Type:** No clicking needed. Just double-tap the **Left Option (⌥)** key to start/stop.
- **Auto-Paste:** Transcribed text is automatically injected into your active application (Slack, Browser, Code Editor, etc.).
- **Local AI Power:** Uses **Whisper Medium** (via MLX) for near-instant, high-accuracy transcription on Apple Silicon.
- **Smart Cleanup:** Uses **Phi-3.5** to fix grammar and punctuation while preserving your original wording and tone.
- **Zero Latency:** Models are pre-warmed on startup so there's no lag on your first use.
- **Environment Presets:** Quickly adjust microphone sensitivity via a web UI to filter out background noise.

## 🚀 Quick Install

1. **Clone the repository:**
   ```bash
   git clone https://github.com/your-username/voice-inject-lite.git
   cd voice-inject-lite
   ```

2. **Run the installer:**
   ```bash
   chmod +x install.sh
   ./install.sh
   ```
   *The installer will ask for your Hugging Face token to download the models.*

## ⚙️ Configuration

Open [http://localhost:3000](http://localhost:3000) in your browser to:
- Monitor recording status.
- Adjust **Mic Environment** presets (Laptop, Office, or Studio).
- View model warmup progress.

## 🔒 Permissions (macOS)

For the application to work, you must grant your Terminal app the following permissions in **System Settings → Privacy & Security**:

1. **Microphone:** Required to hear your voice.
2. **Accessibility:** Required to auto-paste text into other apps.
3. **Input Monitoring:** Required to detect the double-tap hotkey.

## 🛠️ Architecture

- **Whisper (MLX):** Optimized for the M-series Neural Engine.
- **Phi-3.5-mini:** High-performance local LLM for text post-processing.
- **VAD (Voice Activity Detection):** Snappy 300ms silence detection for fast turnovers.
- **Single Stream:** Dedicated worker thread architecture to prevent GPU stream conflicts.

---
Built with ❤️ for Apple Silicon.
