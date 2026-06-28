"""
DoctrineMemory — WorkingMemory with Brain/Seat separation.

Doctrine: learned long-term priors, readable by all loops.
Working: per-step problem state, writable by Brain only.
"""

import math
import torch
import torch.nn as nn
import torch.nn.functional as F


class DoctrineMemory(nn.Module):
    def __init__(self, dim: int, slots: int = 64):
        super().__init__()
        self.dim = dim
        self.slots = slots

        # Doctrine: slow-moving institutional knowledge
        self.doctrine_keys = nn.Parameter(torch.randn(slots, dim) * 0.02)
        self.doctrine_values = nn.Parameter(torch.randn(slots, dim) * 0.02)

        # Working memory: per-step, erased after each forward
        self.write_gate = nn.Linear(dim, dim)
        self.address = nn.Linear(dim, slots)
        self.integrate = nn.Linear(dim * 2, dim)

        # Brain write path (Seat cannot write working memory directly)
        self.brain_write_proj = nn.Linear(dim, dim)

    def forward(self, x: torch.Tensor, brain_mask: torch.Tensor | None = None):
        """
        Args:
            x: [B, T, D] — hidden states from prelude
            brain_mask: [B, T] — 1.0 for positions where Brain is active
        Returns:
            dict with 'doctrine' and 'working' tensors, both [B, T, D]
        """
        B, T, D = x.shape

        # READ doctrine (content-addressable, all loops can read)
        q = x @ self.doctrine_keys.T / math.sqrt(D)  # [B, T, slots]
        read_attn = torch.softmax(q, dim=-1)
        doctrine = read_attn @ self.doctrine_values  # [B, T, D]

        # WRITE working memory (dynamic, per-sequence)
        # Only Brain positions write; if no mask, all positions write
        w = torch.sigmoid(self.address(x))  # [B, T, slots]
        c = torch.tanh(self.write_gate(x))  # [B, T, D]

        if brain_mask is not None:
            c = c * brain_mask.unsqueeze(-1)
            w = w * brain_mask.unsqueeze(-1)

        # Aggregate writes: [B, slots, D]
        write_mem = torch.bmm(w.transpose(1, 2), c)

        # Read back working memory via same attention
        working = torch.bmm(read_attn, write_mem)  # [B, T, D]

        # Fuse: doctrine is always available; working is Brain-contributed
        g = torch.sigmoid(self.integrate(torch.cat([x, doctrine + working], dim=-1)))
        return {
            "doctrine": doctrine,
            "working": working,
            "fused": x + g * (doctrine + working),
        }
