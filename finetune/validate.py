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

DICTATION_SYSTEM = (
    "Rewrite the user's spoken text. Remove hesitations and self-corrections, "
    "keeping only the speaker's final intended meaning. Keep all facts. Fix grammar. "
    "English only. Output only the rewritten text.\n"
    "<critical>Preserve the user's tone. Do not make it more formal.</critical>"
)

REWRITE_SYSTEM = (
    "You are a precision writing tool. Apply the <instruction> to the <content>.\n\n"
    "RULES:\n"
    "1. English ONLY.\n"
    "2. Fix grammar and punctuation.\n"
    "3. Maintain the user's original tone.\n"
    "4. Output ONLY the final text. No explanations, no preamble, no tags."
)

DICTATION_TESTS = [
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

REWRITE_TESTS = [
    {
        "input": "<instruction>make this more concise</instruction>\n<content>\nWe wanted to let you know that we have been thinking about this for a while and we believe the best path forward is to just deprecate the old endpoint and move everyone to v2.\n</content>",
        "expected": "We're deprecating the old endpoint and moving everyone to v2.",
    },
    {
        "input": "<instruction>fix the grammar</instruction>\n<content>\nthe importent thing to remeber is that teh cache invalidaton happens asyncronously\n</content>",
        "expected": "The important thing to remember is that the cache invalidation happens asynchronously.",
    },
    {
        "input": "<instruction>make it less formal keep it casual</instruction>\n<content>\nDear Team, I would like to inform you that the scheduled maintenance has been moved to Sunday. Please adjust accordingly.\n</content>",
        "expected": "Hey team, heads up — maintenance moved to Sunday. Plan accordingly.",
    },
    {
        "input": "<instruction>rewrite this as bullet points</instruction>\n<content>\nThe process involves running tests, building the image, pushing to ECR, and triggering a deploy.\n</content>",
        "expected": "- Run tests\n- Build the image\n- Push to ECR\n- Trigger a deploy",
    },
    {
        "input": "<instruction>make this sound more confident</instruction>\n<content>\nI think maybe we could possibly try adding caching which might help with latency.\n</content>",
        "expected": "We should add caching. It will help with latency.",
    },
]


def run_test(model, tokenizer, system_prompt, test_case):
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": test_case["input"]},
    ]
    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    sampler = make_sampler(temp=0.1)
    response = mlx_generate(model, tokenizer, prompt=prompt, max_tokens=500, sampler=sampler)

    for stop_tag in ["<|im_end|>", "<|end|>", "<|endoftext|>"]:
        if stop_tag in response:
            response = response.split(stop_tag)[0]
    for stop in ["\n(", "\nNote:", "\n---"]:
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

    total = 0
    passed = 0

    print(f"{'═'*60}")
    print(f"  DICTATION CLEANUP TESTS")
    print(f"{'═'*60}")
    for i, test in enumerate(DICTATION_TESTS, 1):
        output = run_test(model, tokenizer, DICTATION_SYSTEM, test)
        match = output.lower().strip(".") == test["expected"].lower().strip(".")
        status = "✅" if match else "⚠️ "
        total += 1

        print(f"\n{'─'*60}")
        print(f"Dictation {i}: {status}")
        print(f"  IN:       {test['input'][:80]}...")
        print(f"  EXPECTED: {test['expected']}")
        print(f"  GOT:      {output}")
        if match:
            passed += 1

    print(f"\n\n{'═'*60}")
    print(f"  REWRITE / INSTRUCTION TESTS")
    print(f"{'═'*60}")
    for i, test in enumerate(REWRITE_TESTS, 1):
        output = run_test(model, tokenizer, REWRITE_SYSTEM, test)
        # For rewrite, compare semantically (lowercase, strip punctuation)
        got_norm = output.lower().strip().rstrip(".")
        exp_norm = test["expected"].lower().strip().rstrip(".")
        match = got_norm == exp_norm
        status = "✅" if match else "⚠️ "
        total += 1

        print(f"\n{'─'*60}")
        print(f"Rewrite {i}: {status}")
        print(f"  INSTRUCTION: {test['input'][:80]}...")
        print(f"  EXPECTED:    {test['expected']}")
        print(f"  GOT:         {output}")
        if match:
            passed += 1

    print(f"\n\n{'═'*60}")
    print(f"  Results: {passed}/{total} exact matches")
    print(f"  (Non-exact outputs above may still be acceptable rewrites)")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    main()
