#!/usr/bin/env bash
# setup-llm-benchmark.sh — set up the optional OpenVINO-GenAI LLM benchmark that
# the stability test uses to report real tokens/sec on the Arc GPU (prefill +
# decode). Idempotent; run as the normal user (NOT root).
#
#   bash scripts/setup-llm-benchmark.sh
#
# It installs a self-contained Python 3.12 env under ~/ovbench via `uv` (the
# distro Python may be too new for OpenVINO wheels), installs openvino-genai, and
# downloads a small INT4 OV model. OpenVINO reaches the Arc GPU through the Intel
# OpenCL runtime (intel-opencl-icd) — no Level Zero package needed. The stress
# test auto-detects ~/ovbench and runs the benchmark with the overclock applied.
set -euo pipefail

MODEL_REPO="${MODEL_REPO:-OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov}"
DIR="$HOME/ovbench"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

[ "$(id -u)" -ne 0 ] || { echo "run as your normal user, not root"; exit 1; }

# apt needs privilege — sudo from a terminal, pkexec when launched from the GUI (no tty)
if [ -t 0 ]; then SUDO="sudo"; else SUDO="pkexec"; fi

echo "==> OpenCL runtime + clpeak (OpenVINO GPU plugin + VRAM-bandwidth benchmark)"
if ! command -v clpeak >/dev/null 2>&1 || ! ls /etc/OpenCL/vendors/*.icd >/dev/null 2>&1; then
  echo "   installing intel-opencl-icd + clpeak (asks for authorization)…"
  $SUDO apt-get install -y intel-opencl-icd clpeak clinfo || true
fi

echo "==> uv + Python 3.12 env"
command -v curl >/dev/null || $SUDO apt-get install -y curl
command -v uv   >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv python install 3.12
uv venv "$DIR" --python 3.12 --seed

echo "==> openvino-genai + huggingface-hub"
"$DIR/bin/pip" install --quiet --disable-pip-version-check openvino openvino-genai huggingface-hub

echo "==> confirm OpenVINO sees the GPU"
"$DIR/bin/python" - <<'PY'
import openvino as ov
c = ov.Core()
print("   OV", ov.__version__, "devices:", c.available_devices)
assert "GPU" in c.available_devices, "GPU not visible to OpenVINO — check intel-opencl-icd"
print("   GPU:", c.get_property("GPU", "FULL_DEVICE_NAME"))
PY

echo "==> download model ($MODEL_REPO)"
"$DIR/bin/python" - "$MODEL_REPO" "$DIR/model" <<'PY'
import sys
from huggingface_hub import snapshot_download
snapshot_download(repo_id=sys.argv[1], local_dir=sys.argv[2])
print("   done")
PY

echo "==> write llmbench.py"
cat > "$DIR/llmbench.py" <<'PY'
import sys, re, openvino_genai as og
model, device = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "GPU")
pipe = og.LLMPipeline(model, device)
cfg = og.GenerationConfig(max_new_tokens=128, ignore_eos=True)
prompt = "Explain how a CPU executes instructions, step by step."
pipe.generate([prompt], cfg)                 # warmup (compiles for the GPU)
res = pipe.generate([prompt], cfg)
m = res.perf_metrics
intok, ttft_ms = m.get_num_input_tokens(), m.get_ttft().mean
print("PREFILL=%.1f" % (intok / (ttft_ms / 1000.0) if ttft_ms > 0 else 0))
print("DECODE=%.1f" % m.get_throughput().mean)
print("INTOK=%d OUTTOK=%d TTFT_ms=%.0f" % (intok, m.get_num_generated_tokens(), ttft_ms))

# Coherence check: an unstable *memory* overclock corrupts data silently — the
# tok/s stay high but the generated text degrades into repetition / gibberish.
# Flag that so a fast-but-wrong overclock gets caught instead of looking like a win.
try:
    text = (res.texts[0] if getattr(res, "texts", None) else str(res)).strip()
except Exception:
    text = ""
words = re.findall(r"\S+", text.lower())
uniq = len(set(words)) / len(words) if words else 0.0
run = best = 1                               # longest immediate-repeat streak
for a, b in zip(words, words[1:]):
    run = run + 1 if a == b else 1
    if run > best: best = run
coherent = 1 if (len(words) >= 8 and uniq >= 0.35 and best <= 6) else 0
print("COHERENT=%d UNIQ=%.2f REP=%d" % (coherent, uniq, best))
PY

echo "==> smoke test on GPU"
"$DIR/bin/python" "$DIR/llmbench.py" "$DIR/model" GPU | grep -E 'PREFILL|DECODE'
echo "==> done. The stability test will now include LLM tok/s."
