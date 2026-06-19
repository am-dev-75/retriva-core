# Copyright (C) 2026 Andrea Marson (am.dev.75@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

#!/usr/bin/env python3
# Copyright (C) 2026 Andrea Marson (am.dev.75@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Smoke Test: Validates Retriva features against Golden Answers.
Uses the configured LLM as a judge to verify factual consistency.
"""

import os
import sys
import re
import argparse
import random
import requests
from pathlib import Path
from typing import List, Tuple

# Setup path to include retriva src
sys.path.append(str(Path(__file__).resolve().parent.parent.parent / "src"))

from retriva.config import settings
from openai import OpenAI
from retriva.logger import get_logger, setup_logging

setup_logging()
logger = get_logger("smoke_test")

# Colors for CLI
GREEN = "\033[92m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"

GOLDEN_ANSWERS_PATH = Path(__file__).resolve().parent.parent.parent / "docs/tests/golden-answers.md"

def parse_golden_answers() -> List[Tuple[str, str]]:
    """
    Parses the markdown file to extract Question/Answer pairs.
    Pattern:
    [Number]. [Question]
    
    [Answer content...]
    
    Sources:
    """
    if not GOLDEN_ANSWERS_PATH.exists():
        logger.debug(f"{RED}Error: Golden answers file not found at {GOLDEN_ANSWERS_PATH}{RESET}")
        return []

    content = GOLDEN_ANSWERS_PATH.read_text()
    
    # Simple regex to find numbered questions
    # Matches "1. Question text" and captures until "Sources:" or next question
    # This is a bit greedy but works for the current format
    q_pattern = re.compile(r"(\d+)\.\s+(.*?)\n\n(.*?)(?=\n\nSources:|\n\n\d+\.)", re.DOTALL)
    
    matches = q_pattern.findall(content)
    qa_pairs = []
    for m in matches:
        number, question, answer = m
        qa_pairs.append((question.strip(), answer.strip()))
        
    return qa_pairs

def judge_answer(question: str, golden: str, generated: str) -> Tuple[bool, str]:
    """
    Uses the configured LLM to judge if the generated answer is factually
    consistent with the golden answer.
    """
    client = OpenAI(api_key=settings.chat_openai_api_key, base_url=settings.chat_base_url)
    
    prompt = f"""You are an objective QA evaluator.
Compare the GENERATED ANSWER against the GOLDEN REFERENCE for the given question.

QUESTION: {question}

GOLDEN REFERENCE:
{golden}

GENERATED ANSWER:
{generated}

Is the GENERATED ANSWER factually consistent with the GOLDEN REFERENCE? 
Consider:
1. Does it contain the same key facts and values (e.g. power consumption numbers)?
2. Does it maintain the same caveats if the information is missing?
3. It does NOT have to be verbatim, just factually equivalent.

Respond ONLY with "YES" or "NO" followed by a short one-sentence explanation.
"""

    try:
        response = client.chat.completions.create(
            model=settings.chat_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0
        )
        result = response.choices[0].message.content.strip()
        is_pass = result.upper().startswith("YES")
        return is_pass, result
    except Exception as e:
        return False, f"Judge error: {str(e)}"

def run_smoke_test():
    parser = argparse.ArgumentParser(description="Retriva Smoke Test")
    parser.add_argument("-r", "--random", action="store_true", help="Execute the test for just one random answer picked from the file.")
    args = parser.parse_args()

    logger.info(f"\n{BOLD}Retriva Smoke Test — Golden Answer Validation{RESET}")
    logger.debug(f"Loading reference: {GOLDEN_ANSWERS_PATH}\n")

    qa_pairs = parse_golden_answers()
    if not qa_pairs:
        logger.debug(f"{RED}No QA pairs found in reference file.{RESET}")
        return

    if args.random:
        qa_pairs = [random.choice(qa_pairs)]

    passed = 0
    total = len(qa_pairs)

    for i, (question, golden) in enumerate(qa_pairs, 1):
        logger.info(f"{BOLD}[{i}/{total}] Testing:{RESET} {question}")
        
        # 1. Run Pipeline
        try:
            payload = {
                "model": settings.chat_model,
                "messages": [{"role": "user", "content": question}],
                "stream": False
            }
            port = settings.openai_api_port
            r = requests.post(f"http://127.0.0.1:{port}/v1/chat/completions", json=payload, timeout=120)
            r.raise_for_status()
            response_data = r.json()
            generated = response_data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.info(f"  {RED}FAILED (Pipeline Error):{RESET} {str(e)}\n")
            continue

        # 2. Judge
        logger.debug(f"  {BOLD}Judging...{RESET}")
        is_pass, explanation = judge_answer(question, golden, generated)

        if is_pass:
            logger.info(f"  {GREEN}PASSED:{RESET} {explanation}\n")
            passed += 1
        else:
            logger.info(f"  {RED}FAILED:{RESET} {explanation}")
            logger.debug(f"\n  {BOLD}--- EXPECTED (GOLDEN) ---{RESET}")
            logger.debug(golden)
            logger.debug(f"\n  {BOLD}--- ACTUAL (GENERATED) ---{RESET}")
            logger.debug(generated)
            logger.debug(f"\n  {BOLD}--------------------------{RESET}\n")

    # Final Summary
    color = GREEN if passed == total else RED
    logger.info(f"{BOLD}Summary:{RESET} {color}{passed}/{total} Passed{RESET}")
    
    if passed == total:
        sys.exit(0)
    else:
        sys.exit(1)

if __name__ == "__main__":
    run_smoke_test()
