"""Make a half-precision (float16) copy of a trained model folder. The original is never changed.

Run:  python training/convert_fp16.py models/intent_model_v2 models/intent_model_v2_fp16
      python training/convert_fp16.py models/entity_model   models/entity_model_fp16

Halves the size on disk (4 bytes -> 2 bytes per weight). Refuses to write the copy if a weight is
too large for float16 (it would become infinity) or if many small weights would be rounded to zero.
Check the copy with training/evaluate_model.py / training/evaluate_ner.py before using it.
"""
import shutil
import sys
from pathlib import Path

import torch
from safetensors.torch import load_file, save_file

FP16_MAX = torch.finfo(torch.float16).max            # 65504
FP16_TINY = torch.finfo(torch.float16).smallest_normal
COPY = ("config.json", "tokenizer.json", "tokenizer_config.json")


def main(src: str, dst: str):
    src_dir, dst_dir = Path(src), Path(dst)
    if not (src_dir / "model.safetensors").is_file():
        raise SystemExit(f"No model.safetensors in {src_dir}")
    if dst_dir.resolve() == src_dir.resolve():
        raise SystemExit("The copy must go to a different folder (the original is never changed).")

    weights = load_file(src_dir / "model.safetensors")
    converted, worst, flushed, total = {}, 0.0, 0, 0
    for name, tensor in weights.items():
        if tensor.is_floating_point():
            worst = max(worst, float(tensor.abs().max()))
            nonzero = tensor != 0
            flushed += int((nonzero & (tensor.abs() < FP16_TINY)).sum())
            total += tensor.numel()
            tensor = tensor.to(torch.float16)
        converted[name] = tensor.contiguous()

    print(f"{src_dir.name}: {total:,} weights, largest |w| = {worst:.3f} (float16 limit {FP16_MAX:.0f}), "
          f"{flushed:,} tiny weights below float16's normal range ({flushed / total:.4%})")
    if worst >= FP16_MAX:
        raise SystemExit("A weight is too large for float16 - copy NOT written.")
    if flushed / total > 0.001:
        raise SystemExit("Too many tiny weights would lose precision - copy NOT written.")

    dst_dir.mkdir(parents=True, exist_ok=True)
    save_file(converted, dst_dir / "model.safetensors", metadata={"format": "pt"})
    for name in COPY:
        shutil.copy2(src_dir / name, dst_dir / name)
    size = lambda p: (p / "model.safetensors").stat().st_size / 1e9   # noqa: E731
    print(f"Wrote {dst_dir}: {size(src_dir):.2f} GB -> {size(dst_dir):.2f} GB (original unchanged)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
