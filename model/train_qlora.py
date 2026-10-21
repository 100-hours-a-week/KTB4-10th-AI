# ruff: noqa: E501  — 코랩 실행 예시·한글 설명 줄이 길다
"""
model/train_qlora.py

자체 모델 v2 — kanana 를 글쓰기(범위 B) 정답 데이터로 SFT 한다. 코랩에서 돈다.
기본은 **bf16 LoRA** 다 (`--quant 4bit` 일 때만 QLoRA). 이름은 옛 파일을 따른 것.
TRL 없이 transformers `Trainer` + peft 로 돈다. SFT 처리는 아래에 직접 짰다.

입력: `label.py build` 가 만든 `{"messages": [system, user, assistant]}` 한 줄.
**레포의 다른 파일을 import 하지 않는다.** 코랩에 이 파일과 `sft_train.jsonl`·`sft_valid.jsonl`만 올리면 돈다.

SFT 규칙 세 가지:
- **loss 는 정답(assistant) 부분에만 건다.** 프롬프트까지 학습시키면 "문제를 만드는 법"을 배운다.
- **정답 끝에 EOS 를 붙인다.** 멈추는 법을 가르치는 한 줄이다.
- **문제지 틀(`build_text`)은 평가·서빙 때와 같아야 한다** — chat template + enable_thinking=False.

코랩 (A100 40GB, bf16):
    !pip install -q uv
    !uv pip install --system -q torch transformers accelerate peft bitsandbytes
    !pip uninstall -y torchao        # peft 가 torchao 에서 ImportError (새 런타임마다)

    !python train_qlora.py --check-only                               # 길이만 잰다
    !python train_qlora.py --out /content/drive/MyDrive/kgb-writer-adapter
    !python train_qlora.py --out /content/drive/MyDrive/kgb-writer-adapter --resume auto   # 끊겼을 때

어댑터만 저장한다(약 170MB). 체크포인트는 25스텝마다 --out 아래에 남는다.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# ============================================================
# 1. 데이터 — 문제지 + 정답 한 쌍을 토큰으로
# ============================================================


def load_rows(path: Path) -> list[dict]:
    """`{"messages": [system, user, assistant]}` 한 줄을 system·prompt·completion 으로 푼다."""
    rows = []  # [{system, prompt, completion}, ...]
    with path.open(encoding="utf-8") as fp:
        for line in fp:
            if not line.strip():
                continue
            system, user, assistant = (
                m["content"] for m in json.loads(line)["messages"]
            )
            rows.append({"system": system, "prompt": user, "completion": assistant})
    return rows


def build_text(tok, system: str, prompt: str) -> str:
    """틀을 씌운 문제지. **`runner.py`의 `generate()`와 같은 모양이어야 한다.**"""
    # system·user 두 칸짜리 대화를 템플릿 문자열로 바꾼다
    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    try:
        return tok.apply_chat_template(
            msgs, add_generation_prompt=True, tokenize=False, enable_thinking=False
        )
    except TypeError:  # enable_thinking 을 모르는 템플릿
        return tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)


def encode(tok, row: dict) -> tuple[list[int], list[int]]:
    """(문제지 토큰, 정답 토큰+EOS). EOS까지 학습시켜야 모델이 멈추는 법을 배운다."""
    head = tok(build_text(tok, row["system"], row["prompt"]), add_special_tokens=False)[
        "input_ids"
    ]
    body = tok(row["completion"], add_special_tokens=False)["input_ids"] + [
        tok.eos_token_id
    ]
    return head, body


class PairSet:
    """문제지 + 모범답안 한 쌍. 프롬프트 자리는 loss에서 빼둔다."""

    def __init__(self, rows: list[dict], tok, max_len: int):
        self.rows = rows
        self.tok = tok
        self.max_len = max_len

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, i: int) -> dict:
        """Trainer 가 샘플 하나를 꺼낼 때 부른다."""
        head, body = encode(self.tok, self.rows[i])
        # -100은 "여기는 채점하지 않는다"는 표시다
        ids = (head + body)[: self.max_len]
        labels = ([-100] * len(head) + body)[: self.max_len]
        return {"input_ids": ids, "labels": labels}


def collate(batch: list[dict], pad_id: int) -> dict:
    """길이를 맞춰 묶는다. 채운 자리는 loss에서 뺀다."""
    import torch

    width = max(len(b["input_ids"]) for b in batch)  # 가장 긴 샘플에 맞춘다
    ids, labels, mask = [], [], []
    for b in batch:
        gap = width - len(b["input_ids"])
        ids.append(b["input_ids"] + [pad_id] * gap)  # 빈자리는 pad 토큰
        labels.append(b["labels"] + [-100] * gap)  # 빈자리는 채점 안 함
        mask.append([1] * len(b["input_ids"]) + [0] * gap)  # 빈자리는 안 봄
    return {
        "input_ids": torch.tensor(ids),
        "labels": torch.tensor(labels),
        "attention_mask": torch.tensor(mask),
    }


def check_lengths(tok, rows: list[dict], max_len: int) -> None:
    """자르면 정답 뒷부분과 EOS가 통째로 날아간다. 학습 전에 확인한다."""
    lens = sorted(
        sum(len(x) for x in encode(tok, r)) for r in rows
    )  # 문제지+정답 토큰 수
    n = len(lens)
    over = sum(1 for x in lens if x > max_len)
    print(
        f"토큰 길이  중앙 {lens[n // 2]:,} · 90% {lens[int(n * 0.9)]:,} · 최대 {lens[-1]:,}"
        f"   (max_seq_len {max_len:,})"
    )
    if over:
        print(
            f"  경고: {over}건이 한도를 넘어 잘린다. "
            f"잘리면 정답 뒷부분이 날아가고 EOS도 사라진다 → --max-len을 {lens[-1] + 64:,} 이상으로"
        )
    else:
        print("  잘리는 샘플 없음")


# ============================================================
# 2. 모델 — kanana 를 얼리고 LoRA 어댑터만 학습
# ============================================================


def load_model(args):
    """kanana 를 GPU 에 올리고 LoRA 어댑터를 붙인다."""
    import torch
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import AutoModelForCausalLM, BitsAndBytesConfig

    # **4bit는 작은 GPU를 위한 것이지 빠르라고 쓰는 게 아니다.** 압축한 가중치를
    # 계산할 때마다 풀어야 해서 매 스텝 비용이 붙는다 (A100에서 실측 48초/스텝).
    # 8B는 bf16으로 16GB라 A100 40GB에 들어가므로 기본은 bf16이다.
    # 베이스라인도 bf16으로 쟀으니 **속도까지 같은 조건에서 비교**할 수 있다.
    kw: dict = {"device_map": "auto"}  # GPU 에 자동 배치
    if args.quant == "4bit":
        kw["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
        )

    try:
        model = AutoModelForCausalLM.from_pretrained(
            args.model, dtype=torch.bfloat16, **kw
        )
    except TypeError:  # transformers 구버전은 `torch_dtype`
        model = AutoModelForCausalLM.from_pretrained(
            args.model, torch_dtype=torch.bfloat16, **kw
        )

    if args.quant == "4bit":  # QLoRA 일 때만 필요한 준비
        model = prepare_model_for_kbit_training(model)
    model.config.use_cache = False  # 체크포인팅과 함께 못 쓴다

    model = get_peft_model(
        model,
        LoraConfig(
            r=args.rank,
            lora_alpha=args.rank * 2,
            lora_dropout=0.05,
            bias="none",
            task_type="CAUSAL_LM",
            # 어텐션과 MLP를 모두 건드린다. 이름은 모델마다 다르므로 peft가 찾게 둔다
            target_modules="all-linear",
        ),
    )
    model.print_trainable_parameters()  # 학습되는 파라미터 수 (v7: 약 4,194만 · 0.52%)
    return model


# ============================================================
# 3. 학습 설정 — transformers 버전마다 인자 이름이 달라 받는 것만 넣는다
# ============================================================


def training_args(args):
    """학습 설정. 지금 설치된 transformers 가 받는 인자만 넣는다."""
    import inspect

    from transformers import TrainingArguments

    # (`evaluation_strategy` → `eval_strategy`, `warmup_ratio` 삭제 등).
    # 모델을 다 받은 뒤에 터지면 손해가 크다.
    accepted = set(inspect.signature(TrainingArguments.__init__).parameters)

    wanted = {
        "output_dir": str(args.out),
        "num_train_epochs": args.epochs,
        # 샘플이 길어(최대 12,288토큰) 한 번에 1개만 올리고, 8번 모아 유효 배치 8로 갱신한다
        "per_device_train_batch_size": 1,
        "per_device_eval_batch_size": 1,
        "gradient_accumulation_steps": args.accum,
        # 메모리를 시간과 맞바꾼다. 메모리가 남으면 꺼서 시간을 되찾는다
        "gradient_checkpointing": not args.no_checkpointing,
        "learning_rate": args.lr,
        "lr_scheduler_type": "cosine",
        "warmup_ratio": 0.03,
        "bf16": True,
        "logging_steps": 10,
        # **에폭 끝에만 저장하면 안 된다.** 코랩은 끊긴다.
        "save_strategy": "steps",
        "save_steps": args.save_steps,
        "save_total_limit": 2,
        "report_to": [],
    }
    # 에폭마다 valid loss 를 잰다 (버전에 따라 이름이 다름)
    for name in ("eval_strategy", "evaluation_strategy"):
        if name in accepted:
            wanted[name] = "epoch"
            break

    dropped = sorted(k for k in wanted if k not in accepted)
    if dropped:
        print(f"이 transformers 버전이 안 받는 설정은 뺐다: {', '.join(dropped)}")
    return TrainingArguments(**{k: v for k, v in wanted.items() if k in accepted})


def find_resume(args) -> str | None:
    """이어받을 체크포인트 경로. `auto` 면 가장 최신을 찾는다."""
    if args.resume != "auto":
        return args.resume
    # `checkpoint-197`처럼 스텝 번호가 붙는다. 숫자로 정렬해야 최신이 잡힌다
    found = sorted(
        args.out.glob("checkpoint-*"), key=lambda p: int(p.name.split("-")[-1])
    )
    resume = str(found[-1]) if found else None
    print(f"이어받기: {resume or '체크포인트 없음 — 처음부터'}")
    return resume


# ============================================================
# 4. 실행
# ============================================================


def parse_args():
    """실행 옵션. 기본값이 v7 학습 설정이다."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="kakaocorp/kanana-1.5-8b-instruct-2505")
    ap.add_argument("--train", type=Path, default=Path("sft_train.jsonl"))
    ap.add_argument("--valid", type=Path, default=Path("sft_valid.jsonl"))
    ap.add_argument("--out", type=Path, default=Path("kgb-writer-adapter"))
    ap.add_argument("--max-len", type=int, default=12288)  # 입력+정답 최대 약 16,500자
    ap.add_argument("--epochs", type=float, default=2)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--accum", type=int, default=8, help="유효 배치 = 이 값")
    ap.add_argument(
        "--quant",
        default="none",
        choices=("none", "4bit"),
        help="4bit는 작은 GPU용. 푸는 비용이 매 스텝 붙어 2~3배 느리다",
    )
    ap.add_argument(
        "--no-checkpointing",
        action="store_true",
        help="그래디언트 체크포인팅을 끈다. 더 빠르고 메모리를 더 쓴다",
    )
    ap.add_argument(
        "--save-steps", type=int, default=25, help="몇 스텝마다 저장할지. 코랩은 끊긴다"
    )
    ap.add_argument(
        "--resume",
        default=None,
        help="이어받을 체크포인트 경로. `auto`면 --out 아래 최신 것을 찾는다",
    )
    ap.add_argument("--check-only", action="store_true", help="길이만 재고 끝낸다")
    return ap.parse_args()


def main() -> None:
    args = parse_args()

    from transformers import AutoTokenizer, Trainer

    # 1) 토크나이저
    tok = AutoTokenizer.from_pretrained(args.model)
    if tok.pad_token_id is None:  # pad 토큰이 없으면 EOS 로 대신
        tok.pad_token = tok.eos_token

    # 2) 데이터 읽기 + 길이 점검
    train_rows = load_rows(args.train)
    valid_rows = load_rows(args.valid)
    print(
        f"학습 {len(train_rows):,}쌍 · 검증 {len(valid_rows):,}쌍 · "
        f"{args.quant} · 에폭 {args.epochs}"
    )
    check_lengths(tok, train_rows, args.max_len)
    if args.check_only:
        return

    # 3) 모델 + LoRA → Trainer 로 학습
    model = load_model(args)
    trainer = Trainer(
        model=model,
        args=training_args(args),
        train_dataset=PairSet(train_rows, tok, args.max_len),
        eval_dataset=PairSet(valid_rows, tok, args.max_len),
        data_collator=lambda b: collate(b, tok.pad_token_id),
    )
    trainer.train(resume_from_checkpoint=find_resume(args))

    # 4) 어댑터만 저장 (약 170MB)
    model.save_pretrained(str(args.out))
    tok.save_pretrained(str(args.out))
    print(f"\n어댑터 저장 → {args.out}")


if __name__ == "__main__":
    main()
