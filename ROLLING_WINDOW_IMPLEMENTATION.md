# Rolling Window Architecture: Implementation Plan

This document tracks the transition of **Voice Inject Lite** from a single-chunk recording model to a continuous rolling-window architecture. This will enable "Infinite Dictation" with near-zero latency and perfect accuracy.

## Goals
- **Infinite Stream:** Remove all hard limits on recording duration.
- **Low Latency:** Text should appear in chunks while the user is still speaking.
- **Microphone Hot-Swap:** The mic never stops listening, even while the AI is thinking.
- **Zero Repetition:** Smart stitching of overlapping audio windows.

---

## Roadmap

### Phase 1: Foundation - Decoupling Recorder & Worker 
- [x] **Step 1.1:** Refactor `client.py` to move audio capture to a dedicated, persistent `AudioRecorder` class.
- [x] **Step 1.2:** Implement a Thread-Safe Audio Queue (using a 10-minute monotonic circular buffer) that allows the worker to "pull" chunks without stopping the stream.
- [x] **Step 1.3:** **Validation:** Verify that speaking while the AI is busy no longer results in lost words.

### Phase 2: Rolling Window Logic 
- [x] **Step 2.1:** Implement a "Windowing Engine" that extracts 15-second windows with a 2-second overlap from the circular buffer.
- [x] **Step 2.2:** Update `mlx_worker` to handle these background chunks without locking the UI.
- [ ] **Step 2.3:** **Validation:** Monitor terminal logs to see "Partial Transcriptions" appearing every 10-15 seconds.

### Phase 3: The Stitching Engine 
- [ ] **Step 3.1:** Develop a text-merging algorithm to identify the "overlap point" between Chunk A and Chunk B.
- [ ] **Step 3.2:** Implement "Stable Point" detection—only paste text that the AI is 100% sure about.
- [ ] **Step 3.3:** **Validation:** Dictate long, multi-sentence paragraphs and check for repeated words at boundaries.

### Phase 4: Continuous Auto-Paste 
- [ ] **Step 4.1:** Enable auto-pasting of "stable" chunks into the active application mid-recording.
- [ ] **Step 4.2:** Implement the final "Flush" logic to clean up any remaining text when the user hits Stop.
- [ ] **Step 4.3:** **Validation:** Full end-to-end test of a 5-minute dictation session.

### Phase 5: Optimization & Speculation 
- [ ] **Step 5.1:** Add "Cancellation Logic"—if new speech is detected, cancel the current LLM cleanup and prioritize the new audio.
- [ ] **Step 5.2:** Fine-tune GPU memory management for long-running sessions.
- [ ] **Step 5.3:** **Final Verification:** Benchmark against the old "Single Chunk" version.

---

## Current Status
- **Current Step:** Phase 2, Step 2.3 (Validation)
- **Status:** Background 'Partial' transcriptions are now being triggered every 10 seconds during long speech.
