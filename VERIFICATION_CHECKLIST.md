# Vigorous Testing Protocol (Pre-Push Checklist)

To prevent regressions and "stuck" bugs, the following manual test suite MUST be performed before pushing any code to the remote repository.

---

## 🟢 Critical Smoke Tests
- [ ] **Startup:** Run `./install.sh`. Does it reach "🔥 Models ready" without a syntax error?
- [ ] **Standard Dictation:** Say "Hello World." Does it paste "Hello world."?
- [ ] **Selection Rewrite:** Highlight "bad grammar text" and say "Fix grammar." Does it replace with perfect English?

## 🟡 Edge-Case Verification
- [ ] **Self-Correction:** Say "Meet at 2... no 3." Does it result in "Meet at 3."?
- [ ] **Rapid Toggling:** Double-tap Start, wait 0.5s, double-tap Stop. Repeat 3 times. Does it hang?
- [ ] **Silence Handling:** Double-tap Start, stay silent for 5 seconds, double-tap Stop. Does it reset the lock correctly?
- [ ] **Long Recording:** Talk for 45 seconds. Does it capture the entire speech without being cut off?

## 🔴 Performance Audit
- [ ] **Handoff Latency:** Is the time between "Stop Blip" and "Text Appearance" under 1.5 seconds?
- [ ] **System Lag:** Open Activity Monitor. Does `Python` usage drop back to near-zero after transcription?

---

## Technical Maintenance
1.  **Check Syntax:** Run `python3 -m py_compile client.py`.
2.  **Verify Models:** Ensure `_MLX_MODEL` and `_LLM_MODEL` strings match available Hugging Face repos.
3.  **Clean Cache:** Periodically check `~/.cache/huggingface` if models fail to load.
