# Engineering Transcript: Voice Inject Lite Optimizations

This document serves as a record of the technical challenges, architectural decisions, and engineering lessons learned during the optimization of the **Voice Inject Lite** project.

---

## 1. The "Hallucination" Defense
**Challenge:** Whisper Large-v3 was "eager to please," often transcribing background noise as common phrases like "Thank you for watching."

**Solution:** 
- **Segment-Level Filtering:** We moved away from just looking at the final text. We now inspect each segment's `avg_logprob` (confidence) and `no_speech_prob`.
- **Log Probability Math:** We learned that models think in log-space ($-\infty$ to $0$) for speed and precision. We set a strict threshold of `-0.6` for Whisper Turbo.
- **Prompt Recitation Check:** We implemented a filter to block the AI if it simply repeated words from the custom vocabulary without any new context.

## 2. The "Hanging" Bug (Race Conditions)
**Challenge:** The application would frequently "freeze" after rapid hotkey presses.

**Solution:**
- **Leaked Locks:** We identified that "Early Returns" in Python were skipping the line that reset the `_is_processing` lock.
- **Manual Stop Priority:** We refactored the hotkey logic to ensure a manual "Stop" always overrides background AI tasks.
- **Try/Finally Resilience:** We wrapped transcription and LLM callbacks in `try/finally` blocks to GUARANTEE the lock is released, even if the AI crashes or the clipboard fails.

## 3. Latency vs. Intelligence
**Challenge:** High accuracy usually meant high latency (Large-v3 + LLM cleanup = ~5s wait).

**Solution:**
- **The Turbo Switch:** Moved to `Whisper Large-v3-turbo`, which is 3.5x faster on Apple Silicon with negligible accuracy loss.
- **VAD Memory Leak:** Found that re-concatenating the entire audio buffer every 100ms was choking the CPU. Optimized it to only scan the most recent 100ms "peek."
- **Instruction Resolution:** Discovered that the LLM (Qwen 2.5) is essential for fixing "self-corrections" (e.g., "Meet at 2... no 3"). We kept the LLM but shortened the system prompt to 3 rules to keep generation under 1 second.

## 4. Architectural Evolution: Rolling Windows
**Challenge:** Users were being cut off by hard limits on audio duration.

**Solution:**
- **Decoupled Stream:** Refactored the app into a persistent `AudioRecorder` with a monotonic circular buffer.
- **Hot-Mic Recording:** The mic now records continuously, even while the AI is thinking, so you never lose the first word of your next sentence.
- **Intent Preservation:** We decided to wait for a manual stop before pasting to allow the LLM to resolve complex thoughts, rather than pasting "dirty" partial chunks.

---

## Future Failure Points (What could still go wrong?)
1.  **Hugging Face Tokens:** Tokens expire or lose permissions.
2.  **OS Updates:** macOS updates often break `osascript` (AppleScript) or `pynput` permissions.
3.  **Model drift:** If Hugging Face updates the model files, `mlx-lm` might require a code update to load them.
4.  **RAM Pressure:** Running a Large LLM and Whisper simultaneously uses ~6-8GB of GPU RAM. On a 16GB machine, this is fine, but it leaves little room for other heavy apps.
