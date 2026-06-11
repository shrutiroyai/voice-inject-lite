"""
Validate the fine-tuned model against test cases.

Usage:
    .venv/bin/python3 finetune/validate.py                    # test the fused model
    .venv/bin/python3 finetune/validate.py --base             # test the base model (for comparison)
"""

import argparse
from pathlib import Path
from mlx_lm import load, generate as mlx_generate
from mlx_lm.sample_utils import make_sampler

SCRIPT_DIR = Path(__file__).parent
FUSED_DIR = str(SCRIPT_DIR / "fused_model")
BASE_MODEL = "mlx-community/Qwen2.5-1.5B-Instruct-4bit"

SYSTEM_PROMPT = (
    "Rewrite the user's spoken text. Remove hesitations and self-corrections, "
    "keeping only the speaker's final intended meaning. Keep all facts. Fix grammar. "
    "English only. Output only the rewritten text.\n"
    "<critical>Preserve the user's tone. Do not make it more formal.</critical>"
)

TEST_CASES = [
    {
        "input": "I also started the training job. So, we should see the bullseye by the end of today or actually by the end of midday today. Sorry, no wait. It should be done by noon today.",
        "expected": "I also started the training job. We should see the bullseye by noon today.",
    },
    {
        "input": "so i was thinking we should probably go to the store and uh get some stuff for the party no wait actually lets just order it online",
        "expected": "So I was thinking we should probably just order stuff for the party online.",
    },
    {
        "input": "can you send me the document I need it by tomorrow morning actually no by end of day today",
        "expected": "Can you send me the document? I need it by end of day today.",
    },
    {
        "input": "the deployment finished at like 3 am and everything looks good so far no issues",
        "expected": "The deployment finished at 3 am and everything looks good so far, no issues.",
    },
    {
        "input": "I talked to the PM and she said the launch is March 15 oh wait no she said March 20",
        "expected": "I talked to the PM and she said the launch is March 20.",
    },
]


def run_test(model, tokenizer, test_case):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": test_case["input"]},
    ]
    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    sampler = make_sampler(temp=0.1)
    response = mlx_generate(model, tokenizer, prompt=prompt, max_tokens=500, sampler=sampler)

    for stop_tag in ["<|im_end|>", "<|end|>", "<|endoftext|>"]:
        if stop_tag in response:
            response = response.split(stop_tag)[0]
    for stop in ["\n(", "\n\n", "\nNote:", "\n---"]:
        if stop in response:
            response = response.split(stop)[0]

    return response.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", action="store_true", help="Test base model instead of fine-tuned")
    args = parser.parse_args()

    model_path = BASE_MODEL if args.base else FUSED_DIR
    label = "BASE" if args.base else "FINE-TUNED"

    print(f"Loading {label} model: {model_path}")
    model, tokenizer = load(model_path)
    print(f"Loaded.\n")

    passed = 0
    for i, test in enumerate(TEST_CASES, 1):
        output = run_test(model, tokenizer, test)
        match = output.lower().strip(".") == test["expected"].lower().strip(".")
        status = "✅" if match else "⚠️ "

        print(f"{'─'*60}")
        print(f"Test {i}: {status}")
        print(f"  IN:       {test['input']}")
        print(f"  EXPECTED: {test['expected']}")
        print(f"  GOT:      {output}")
        if match:
            passed += 1

    print(f"\n{'═'*60}")
    print(f"  Results: {passed}/{len(TEST_CASES)} exact matches")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    main()
