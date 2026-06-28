"""
OpenMythos — Governed Recurrent Reasoning Engine.

Architecture:
    Prelude (standard transformer blocks)
      ↓
    DoctrineMemory (doctrine + working memory)
      ↓
    BrainCouncil (Logic/Seat/Adversary/Verifier with governance)
      ↓
    Coda (standard transformer blocks)
      ↓
    LM Head + CalibratedHead

Governance doctrine:
    - Seat (prob_loop) owns execution authority. Always the base.
    - Brain (logic_loop) is advisory only. Never punished, never leaks into Seat.
    - RoadGuard (adversary_loop) is binary BLOCKED/OPEN.
    - Verifier (verifier_loop) scores P&L of other loops.
    - Governor moderates advisor influence via structured modifiers.

RoadGuard penalty applied to:
    - confidence: confidence * (1.0 - 0.50 * block_mask)
    - logits: logits - block_mask * risk_penalty
"""

import math
from dataclasses import dataclass
from typing import Optional, Dict, Any

import torch
import torch.nn as nn
import torch.nn.functional as F

from .memory import DoctrineMemory
from .governance import BrainCouncil, RMSNorm
from .calibration import CalibratedHead


@dataclass
class OpenMythosConfig:
    dim: int = 1024
    n_layers: int = 24
    n_heads: int = 16
    vocab_size: int = 32000
    max_seq_len: int = 8192
    rope_theta: float = 10000.0
    n_loops: int = 2
    memory_slots: int = 64
    recurrent_block_cls: Any = None
    risk_penalty: float = 1.0  # logits penalty when BLOCKED


class OpenMythos(nn.Module):
    def __init__(self, cfg: OpenMythosConfig):
        super().__init__()
        self.cfg = cfg

        self.embed = nn.Embedding(cfg.vocab_size, cfg.dim)

        prelude_depth = cfg.n_layers // 3
        self.prelude = nn.ModuleList([
            cfg.recurrent_block_cls(cfg) for _ in range(prelude_depth)
        ])

        self.memory = DoctrineMemory(cfg.dim, slots=cfg.memory_slots)
        self.council = BrainCouncil(cfg)

        coda_depth = cfg.n_layers - prelude_depth
        self.coda = nn.ModuleList([
            cfg.recurrent_block_cls(cfg) for _ in range(coda_depth)
        ])

        self.norm = RMSNorm(cfg.dim)
        self.head = nn.Linear(cfg.dim, cfg.vocab_size, bias=False)
        self.calibrated_head = CalibratedHead(cfg.dim)

        self.head.weight = self.embed.weight
        self._init_weights()

    def _init_weights(self):
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def build_freqs_cis(self, seq_len: int, device: torch.device):
        dim = self.cfg.dim // self.cfg.n_heads
        theta = self.cfg.rope_theta
        freqs = 1.0 / (theta ** (torch.arange(0, dim, 2, device=device).float() / dim))
        t = torch.arange(seq_len, device=device, dtype=freqs.dtype)
        freqs = torch.outer(t, freqs)
        return torch.polar(torch.ones_like(freqs), freqs)

    def forward(
        self,
        input_ids: torch.Tensor,
        n_loops: Optional[int] = None,
        kv_cache: Optional[Dict[str, torch.Tensor]] = None,
        start_pos: int = 0,
        return_trace: bool = False,
    ):
        B, T = input_ids.shape
        n_loops = n_loops or self.cfg.n_loops
        device = input_ids.device

        x = self.embed(input_ids)

        seq_len = start_pos + T
        freqs_cis = self.build_freqs_cis(seq_len, device)
        freqs_cis = freqs_cis[start_pos:start_pos + T]

        mask = torch.triu(torch.ones(T, T, device=device), diagonal=1).bool()
        mask = mask.unsqueeze(0).unsqueeze(0)

        for i, layer in enumerate(self.prelude):
            x = layer(x, freqs_cis=freqs_cis, mask=mask, kv_cache=kv_cache, cache_key=f"prelude_{i}")

        mem_out = self.memory(x)
        x = mem_out["fused"]
        e = x

        x, trace = self.council(x, e, freqs_cis, mask, n_loops, kv_cache)

        for i, layer in enumerate(self.coda):
            x = layer(x, freqs_cis=freqs_cis, mask=mask, kv_cache=kv_cache, cache_key=f"coda_{i}")

        hidden = self.norm(x)
        logits = self.head(hidden)

        if not return_trace:
            return logits

        cal = self.calibrated_head(hidden)
        confidence = cal["confidence"]  # [B, T]
        uncertainty = cal["uncertainty"]  # [B, T]

        # RoadGuard penalties applied to OUTPUTS, not hidden state
        block_mask = trace["roadguard_block_mask"]  # [B, 1]

        # Confidence penalty: reduce confidence when BLOCKED
        confidence = confidence * (1.0 - 0.50 * block_mask.view(-1, 1))

        # Logits penalty: push toward safe/base when BLOCKED
        risk_penalty = self.cfg.risk_penalty
        logits = logits - block_mask.view(-1, 1, 1) * risk_penalty

        return {
            "logits": logits,
            "confidence": confidence,
            "raw_confidence": cal["confidence"],
            "uncertainty": uncertainty,
            "trace": trace,
        }

    @torch.no_grad()
    def generate(
        self,
        input_ids: torch.Tensor,
        max_new_tokens: int = 128,
        temperature: float = 1.0,
        top_k: Optional[int] = None,
        n_loops: Optional[int] = None,
    ):
        self.eval()
        B, T = input_ids.shape
        device = input_ids.device
        kv_cache = {}

        for pos in range(max_new_tokens):
            out = self.forward(
                input_ids,
                n_loops=n_loops,
                kv_cache=kv_cache,
                start_pos=T + pos,
                return_trace=True,
            )
            logits = out["logits"][:, -1, :] / temperature

            if top_k is not None:
                v, _ = torch.topk(logits, min(top_k, logits.size(-1)))
                logits[logits < v[:, [-1]]] = -float("Inf")

            probs = F.softmax(logits, dim=-1)
            next_token = torch.multinomial(probs, num_samples=1)
            input_ids = torch.cat([input_ids, next_token], dim=1)

            if pos == max_new_tokens - 1:
                return {
                    "tokens": input_ids,
                    "trace": out["trace"],
                    "confidence": out["confidence"][:, -1],
                    "uncertainty": out["uncertainty"][:, -1],
                }

        return {"tokens": input_ids}
