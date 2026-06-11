# 🎙️ Voice Inject Lite

A high-performance, local speech-to-text injector for macOS. Speak naturally, and have the text auto-pasted directly into any application with intelligent grammar, punctuation, and intent resolution.

## ✨ Key Features

- **Infinite Stream (Rolling Window):** No more hard limits. Talk for minutes; the mic never stops listening.
- **Turbo Speed:** Uses `Whisper Large-v3-turbo` for near-instant 3.5x faster transcription on Apple Silicon.
- **Smart Intent Resolution:** Resolves self-corrections (e.g., "Let's meet at 2... no 3") into concise, perfect text.
- **Double-Tap Hotkey:** Minimalist "Left Option (⌥)" trigger for distraction-free dictation.
- **Selection Mode:** Highlight any text and say a command (e.g., "Make this friendly") to rewrite it instantly.

## 🛠️ Performance Architecture

1.  **Monotonic Circular Buffer:** The mic records 100% of the time into a 10-minute loop, ensuring no words are lost during AI processing.
2.  **Dual AI Engine:** Combines the speed of Whisper Turbo with the intelligence of Qwen 2.5 (1.5B) for perfect cleanup.
3.  **Low Latency Handoff:** Bypasses slow OS scripts for instant selection capture and clipboard pasting.

## 🚀 Setup & Usage

1.  **Install:** Run `./install.sh` and follow the instructions.
2.  **Hotkey:** Double-tap **Left Option (⌥)** to start. Double-tap again to stop and paste.
3.  **Permissions:** Ensure your Terminal has **Microphone** and **Accessibility** permissions in System Settings.

---
Built with ❤️ for speed and privacy. 
