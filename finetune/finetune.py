"""
Fine-tune Qwen 2.5-1.5B for speech cleanup using MLX LoRA.

Usage:
    cd voice-inject-lite
    .venv/bin/python3 finetune/finetune.py

What this does:
    1. Loads the base Qwen 2.5-1.5B-Instruct-4bit model
    2. Applies LoRA adapters (trains only ~1% of parameters)
    3. Trains on examples in train.jsonl (~5-10 minutes on M-series Mac)
    4. Saves adapters to finetune/adapters/
    5. Fuses adapters into a standalone model at finetune/fused_model/

After fine-tuning, update _LLM_MODEL in client.py to point to the fused model path.
"""

import subprocess
import sys
import os
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
BASE_MODEL = "mlx-community/Qwen2.5-1.5B-Instruct-4bit"
TRAIN_DATA = SCRIPT_DIR / "train.jsonl"
ADAPTER_DIR = SCRIPT_DIR / "adapters"
FUSED_DIR = SCRIPT_DIR / "fused_model"

LORA_CONFIG = {
    "iters": 200,
    "learning_rate": 1e-5,
    "batch_size": 1,
    "lora_layers": 8,
    "lora_rank": 8,
}


def run_cmd(cmd, desc):
    print(f"\n{'='*50}")
    print(f"  {desc}")
    print(f"{'='*50}\n")
    print(f"$ {' '.join(cmd)}\n")
    result = subprocess.run(cmd, cwd=str(SCRIPT_DIR))
    if result.returncode != 0:
        print(f"\n❌ Failed: {desc}")
        sys.exit(1)
    print(f"\n✅ {desc} complete")


def main():
    print("🎯 Fine-tuning Qwen 2.5-1.5B for speech cleanup")
    print(f"   Base model:    {BASE_MODEL}")
    print(f"   Training data: {TRAIN_DATA}")
    print(f"   Iterations:    {LORA_CONFIG['iters']}")
    print(f"   LoRA rank:     {LORA_CONFIG['lora_rank']}")
    print(f"   LoRA layers:   {LORA_CONFIG['lora_layers']}")

    num_examples = sum(1 for _ in open(TRAIN_DATA))
    print(f"   Examples:      {num_examples}")

    # Step 1: Train LoRA adapters
    train_cmd = [
        sys.executable, "-m", "mlx_lm.lora",
        "--model", BASE_MODEL,
        "--train",
        "--data", str(SCRIPT_DIR),
        "--adapter-path", str(ADAPTER_DIR),
        "--iters", str(LORA_CONFIG["iters"]),
        "--learning-rate", str(LORA_CONFIG["learning_rate"]),
        "--batch-size", str(LORA_CONFIG["batch_size"]),
        "--lora-layers", str(LORA_CONFIG["lora_layers"]),
        "--lora-rank", str(LORA_CONFIG["lora_rank"]),
    ]
    run_cmd(train_cmd, "Training LoRA adapters")

    # Step 2: Fuse adapters into a standalone model
    fuse_cmd = [
        sys.executable, "-m", "mlx_lm.fuse",
        "--model", BASE_MODEL,
        "--adapter-path", str(ADAPTER_DIR),
        "--save-path", str(FUSED_DIR),
    ]
    run_cmd(fuse_cmd, "Fusing adapters into standalone model")

    print(f"\n{'='*50}")
    print(f"  🎉 Fine-tuning complete!")
    print(f"{'='*50}")
    print(f"\n  Fused model saved to: {FUSED_DIR}")
    print(f"\n  To use it, update client.py:")
    print(f'    _LLM_MODEL = "{FUSED_DIR}"')
    print(f"\n  To upload to Hugging Face:")
    print(f"    huggingface-cli upload YOUR_USERNAME/Qwen2.5-1.5B-voice-inject {FUSED_DIR}")
    print()


if __name__ == "__main__":
    main()
