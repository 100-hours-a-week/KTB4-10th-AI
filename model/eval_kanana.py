# ruff: noqa: E501  — 코랩 실행 예시·한글 설명 줄이 길다
"""
model/eval_kanana.py — 코랩(A100 40GB)에서 돈다. 레포를 import 하지 않는다.

7단계 오프라인 평가. eval_inputs.jsonl(test 200)을 학습 전 kanana 와 학습한 kanana(어댑터) 둘 다에 넣는다.
greedy decoding(temperature 0), 요청마다 1개. JSON 모양은 교사 때처럼 vLLM 구조화 출력으로 강제한다
— 형식이 아니라 글을 비교하려고.

    !pip install -q uv && uv pip install --system -q vllm
    !pip uninstall -y torchaudio        # vLLM 설치 후 import 에러 (progress 8장)
    from google.colab import drive; drive.mount('/content/drive')
    !python eval_kanana.py /content/drive/MyDrive/kgb-writer-adapter
    !python eval_kanana.py /content/drive/MyDrive/kgb-writer-adapter 0.3 0.5   # 어댑터만, frequency_penalty 별로

결과: /content/eval_base.jsonl · /content/eval_lora.jsonl (어댑터 폴더 옆에도 복사)
penalty 를 주면 /content/eval_lora_fp0.3.jsonl 처럼 값마다 한 파일. 10/6 첫 평가에서 '같은 틀' 반복이 많아 시험한다.
→ 맥의 fixtures/model_v2/ 로 내려받아 eval.py check.
seconds 는 200건을 한꺼번에 돌린 시간 ÷ 200 이라 속도 비교용 참고값이다. TTFT 는 서빙 환경에서 따로 잰다.
"""

import json
import os
import shutil
import sys
import time

from vllm import LLM, SamplingParams
from vllm.lora.request import LoRARequest

BASE = "kakaocorp/kanana-1.5-8b-instruct-2505"
ADAPTER = sys.argv[1]
PENALTIES = [float(p) for p in sys.argv[2:]]

rows = [json.loads(line) for line in open("eval_inputs.jsonl", encoding="utf-8")]
schema = rows[0]["schema"]
try:  # vLLM 0.11 부터 structured_outputs
    from vllm.sampling_params import StructuredOutputsParams

    guided = {"structured_outputs": StructuredOutputsParams(json=schema)}
except ImportError:
    from vllm.sampling_params import GuidedDecodingParams

    guided = {"guided_decoding": GuidedDecodingParams(json=schema)}

llm = LLM(
    BASE,
    enable_lora=True,
    max_lora_rank=16,
    max_model_len=12288,
    gpu_memory_utilization=0.9,
)
messages = [
    [{"role": "system", "content": r["system"]}, {"role": "user", "content": r["user"]}]
    for r in rows
]

# 학습 때 문제지 틀(train_qlora.build_text)과 같게 enable_thinking=False
lora = LoRARequest("writer", 1, ADAPTER)
runs = [(f"lora_fp{p}", lora, p) for p in PENALTIES] or [
    ("base", None, 0.0),
    ("lora", lora, 0.0),
]
for name, lora, penalty in runs:
    params = SamplingParams(
        temperature=0, frequency_penalty=penalty, max_tokens=4096, **guided
    )
    began = time.monotonic()
    outputs = llm.chat(
        messages,
        params,
        lora_request=lora,
        chat_template_kwargs={"enable_thinking": False},
    )
    seconds = round((time.monotonic() - began) / len(rows), 2)

    out = f"/content/eval_{name}.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for r, o in zip(rows, outputs):
            row = {
                "request_id": r["request_id"],
                "model": f"kanana-{name}",
                "text": o.outputs[0].text,
                "seconds": seconds,
                "output_tokens": len(o.outputs[0].token_ids),
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    shutil.copy(out, os.path.dirname(ADAPTER.rstrip("/")))
    print(f"{name} 끝 (요청당 {seconds}초) → {out}")
