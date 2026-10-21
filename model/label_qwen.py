"""
model/label_qwen.py — 코랩(A100 40GB)에서 돈다. 레포를 import 하지 않는다.

5단계 정답 글 채우기. label_inputs.jsonl 의 요청마다 교사(Qwen3-32B-AWQ)로 답을 N개 뽑는다.
고르는 일은 맥의 label.py build 가 한다. 설정은 4단계 시험(pilot_qwen.py)과 같다.

    !pip install -q uv && uv pip install --system -q vllm
    from google.colab import drive; drive.mount('/content/drive')          # 끊겨도 남게 (선택)
    !python label_qwen.py /content/drive/MyDrive/kgb_label                 # 드라이브 폴더는 선택
    !python label_qwen.py /content/drive/MyDrive/kgb_label_rest label_inputs_rest.jsonl   # 10/6 나머지 1,600
    !python label_qwen.py /content/drive/MyDrive/kgb_label_v6 label_inputs_v6.jsonl 8     # 10/9 2차 프롬프트, 요청마다 8개

CHUNK 건씩 돌리고 끝날 때마다 /content/label_outputs.jsonl 에 이어 쓴 뒤 드라이브로 복사한다.
다시 실행하면 이미 있는 request_id 는 건너뛴다 — 끊기면 드라이브의 파일을 /content 로 되돌리고 다시 실행한다.
"""

import json
import os
import shutil
import sys
import time

from vllm import LLM, SamplingParams

MODEL = "Qwen/Qwen3-32B-AWQ"
N = int(sys.argv[3]) if len(sys.argv) > 3 else 4  # 요청마다 뽑는 답 수 (10/9 v6 생성은 8)
CHUNK = 100
OUT = "/content/label_outputs.jsonl"
BACKUP = sys.argv[1] if len(sys.argv) > 1 else None
INPUT = sys.argv[2] if len(sys.argv) > 2 else "label_inputs.jsonl"

rows = [json.loads(line) for line in open(INPUT, encoding="utf-8")]
done = set()
if os.path.exists(OUT):
    done = {json.loads(line)["request_id"] for line in open(OUT, encoding="utf-8")}
todo = [r for r in rows if r["request_id"] not in done]
print(f"전체 {len(rows)} · 끝난 것 {len(done)} · 남은 것 {len(todo)}")

schema = rows[0]["schema"]
try:  # vLLM 0.11 부터 structured_outputs
    from vllm.sampling_params import StructuredOutputsParams
    guided = {"structured_outputs": StructuredOutputsParams(json=schema)}
except ImportError:
    from vllm.sampling_params import GuidedDecodingParams
    guided = {"guided_decoding": GuidedDecodingParams(json=schema)}

# Qwen 권장값(비사고 모드): temperature 0.7, top_p 0.8
params = SamplingParams(n=N, temperature=0.7, top_p=0.8, max_tokens=8000, **guided)
llm = LLM(MODEL, max_model_len=16384, gpu_memory_utilization=0.92)

for start in range(0, len(todo), CHUNK):
    chunk = todo[start:start + CHUNK]
    messages = [[{"role": "system", "content": r["system"]}, {"role": "user", "content": r["user"]}] for r in chunk]
    began = time.monotonic()
    outputs = llm.chat(messages, params, chat_template_kwargs={"enable_thinking": False})
    seconds = round((time.monotonic() - began) / len(chunk), 1)

    with open(OUT, "a", encoding="utf-8") as f:
        for r, o in zip(chunk, outputs):
            for i, sample in enumerate(o.outputs):
                row = {"request_id": r["request_id"], "sample": i, "model": MODEL, "text": sample.text,
                       "seconds": seconds, "output_tokens": len(sample.token_ids)}
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    if BACKUP:
        os.makedirs(BACKUP, exist_ok=True)
        shutil.copy(OUT, BACKUP)
    print(f"{start + len(chunk)}/{len(todo)} 끝 (요청당 {seconds}초)")

print(OUT)
