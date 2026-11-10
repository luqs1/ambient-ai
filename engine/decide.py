"""Local decision models: tiny classifiers, not LLMs.

  Judge     zero-shot NLI classifier (DeBERTa-v3-xsmall, ~70 MB ONNX int8). Given a
            context and plain-English statements, returns P(statement is true).
            Used for typed judgments: "this task needs someone's phone number",
            "this message asks me to do something".
  Embedder  bge-small (ONNX) sentence embeddings, for clustering related events.

Both run on CPU through onnxruntime in a few ms to tens of ms, load lazily on first
use, and never raise into callers: on any failure they return None and callers fall
back to rules.
"""
from __future__ import annotations

import json
import os
import threading
from functools import lru_cache
from pathlib import Path

import numpy as np

JUDGE_REPO = "MoritzLaurer/deberta-v3-xsmall-zeroshot-v1.1-all-33"
EMBED_REPO = "Xenova/bge-small-en-v1.5"
MODEL_FILES = ("tokenizer.json", "config.json", "onnx/model_quantized.onnx")   # per repo; packaging ships exactly these
_lock = threading.Lock()


def _dl(repo: str, filename: str) -> str:
    # Packaged app: the models ship inside it (AMBIENT_MODELS_DIR/<repo>/<file>), so nothing is fetched.
    bundled = os.environ.get("AMBIENT_MODELS_DIR")
    if bundled and (Path(bundled) / repo / filename).is_file():
        return str(Path(bundled) / repo / filename)
    from huggingface_hub import hf_hub_download
    try:
        return hf_hub_download(repo, filename, local_files_only=True)
    except Exception:
        if bundled:
            raise                                # packaged app: never fetch
        return hf_hub_download(repo, filename)   # dev, first run only: fetch once, then always local


def _fetch(repo: str, files: list[str]) -> Path:
    from huggingface_hub import hf_hub_download
    path = None
    for f in files:
        path = hf_hub_download(repo, f)
    return Path(path).parent.parent if "/" in files[-1] else Path(path).parent


class _Model:
    def __init__(self, repo: str, onnx_file: str):
        # onnxruntime reports usage to Microsoft (1DS) unless told not to; nothing leaves this machine by default.
        os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.tok = Tokenizer.from_file(_dl(repo, "tokenizer.json"))
        self.config = json.loads(Path(_dl(repo, "config.json")).read_text())
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        self.sess = ort.InferenceSession(_dl(repo, onnx_file), opts,
                                         providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}

    def _feed(self, encs, max_len: int) -> dict:
        n = min(max(len(e.ids) for e in encs), max_len)
        ids = np.zeros((len(encs), n), dtype=np.int64)
        mask = np.zeros_like(ids)
        types = np.zeros_like(ids)
        for i, e in enumerate(encs):
            k = min(len(e.ids), n)
            ids[i, :k] = e.ids[:k]
            mask[i, :k] = e.attention_mask[:k]
            types[i, :k] = e.type_ids[:k]
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = types
        return feed


class Judge(_Model):
    def __init__(self):
        super().__init__(JUDGE_REPO, "onnx/model_quantized.onnx")
        labels = {v.lower(): int(k) for k, v in self.config["id2label"].items()}
        self.entail = labels.get("entailment", 0)

    def probs(self, context: str, statements: list[str]) -> list[float]:
        self.tok.enable_truncation(max_length=512, strategy="only_first")
        encs = self.tok.encode_batch([(context, s) for s in statements])
        logits = self.sess.run(None, self._feed(encs, 512))[0]
        e = np.exp(logits - logits.max(axis=1, keepdims=True))
        p = e / e.sum(axis=1, keepdims=True)
        return [float(x) for x in p[:, self.entail]]


class Embedder(_Model):
    def __init__(self):
        super().__init__(EMBED_REPO, "onnx/model_quantized.onnx")

    def embed(self, texts: list[str]) -> np.ndarray:
        self.tok.enable_truncation(max_length=256)
        encs = self.tok.encode_batch(texts)
        out = self.sess.run(None, self._feed(encs, 256))[0]
        v = out[:, 0, :]  # CLS pooling, as bge expects
        return v / np.linalg.norm(v, axis=1, keepdims=True)


_judge: Judge | None = None
_embedder: Embedder | None = None
_failed: set[str] = set()


def judge() -> Judge | None:
    global _judge
    if _judge is None and "judge" not in _failed:
        with _lock:
            if _judge is None:
                try:
                    _judge = Judge()
                except Exception:
                    _failed.add("judge")
    return _judge


def embedder() -> Embedder | None:
    global _embedder
    if _embedder is None and "embed" not in _failed:
        with _lock:
            if _embedder is None:
                try:
                    _embedder = Embedder()
                except Exception:
                    _failed.add("embed")
    return _embedder


def probs(context: str, statements: list[str]) -> list[float] | None:
    j = judge()
    if not j or not context or not statements:
        return None
    try:
        return j.probs(context[:4000], statements)
    except Exception:
        return None


@lru_cache(maxsize=4096)
def prob(context: str, statement: str) -> float | None:
    r = probs(context, [statement])
    return r[0] if r else None
