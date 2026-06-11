# Short Utterance Fixes (experimental - remove if not working)

## Changes made to client.py

### 1. Fallback to raw transcription when segment filter rejects all
- **Location**: ~line 505 (after segment filtering in mlx_worker transcribe block)
- **Change**: If segment filter produces empty text but Whisper DID produce text, use raw text
- **Reason**: Short utterances get low confidence scores but are still real speech. The hallucination word list is the real safety net.

### 2. Peak RMS instead of full-buffer RMS
- **Location**: ~line 878 (command_flush_remaining energy check)
- **Change**: Instead of averaging RMS over entire buffer, finds the loudest 0.5s window
- **Reason**: Short phrases surrounded by silence drag the average RMS below threshold
