"""
BrainCouncil — Governed recurrent reasoning engine.

Doctrine mapping:
  logic_loop    → Brain (doctrine, advisory only)
  prob_loop     → Seat (execution authority, owns logits)
  adversary_loop → RoadGuard (threat detector, binary BLOCKED/OPEN)
  verifier_loop  → Verifier (P&L scoring, promotes/demotes loops)
  governor       → Governor (structured modifiers, vote gating)

Rule: Seat is always the base. Advisors are bounded modifiers.
RoadGuard suppresses all advisor influence when BLOCKED.
Only the Seat-governed stream reaches logits.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F


class RMSNorm(nn.Module):
    def __init__(self, dim: int, eps: float = 1e-6):
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def forward(self, x):
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps) * self.weight


class GovernorRouter(nn.Module):
    """Governor: returns structured modifiers with vote gating."""

    def __init__(self, dim: int):
        super().__init__()
        self.norm = RMSNorm(dim)
        self.weight_proj = nn.Linear(dim, 4, bias=False)
        self.gate_proj = nn.Linear(dim, 4, bias=False)
        self.reason_proj = nn.Linear(dim, dim, bias=False)

    def forward(self, x: torch.Tensor):
        h = self.norm(x[:, -1])
        raw_weights = F.softplus(self.weight_proj(h))
        gates = torch.sigmoid(self.gate_proj(h))
        weights = raw_weights * gates
        weights = weights / (weights.sum(dim=-1, keepdim=True) + 1e-6)
        return {
            "weights": weights,
            "gates": gates,
            "reason_latent": self.reason_proj(h),
        }


class RoadGuard(nn.Module):
    """RoadGuard: binary only. BLOCKED suppresses all advisor influence."""

    def __init__(self, dim: int, block_threshold: float = 0.5):
        super().__init__()
        self.norm = RMSNorm(dim)
        self.block_proj = nn.Linear(dim, 1, bias=True)
        self.block_threshold = block_threshold

    def forward(self, x):
        h = self.norm(x[:, -1])
        block_prob = torch.sigmoid(self.block_proj(h))
        hard = (block_prob > self.block_threshold).float()
        block_mask = hard + block_prob - block_prob.detach()
        return block_mask, block_prob


class VerifierHead(nn.Module):
    """Verifier: scores each loop's P&L and emits confidence."""

    def __init__(self, dim: int):
        super().__init__()
        self.norm = RMSNorm(dim)
        self.score_proj = nn.Linear(dim, 4, bias=False)
        self.confidence_proj = nn.Linear(dim, 1, bias=False)

    def forward(self, x: torch.Tensor):
        h = self.norm(x[:, -1])
        return {
            "loop_scores": self.score_proj(h),
            "confidence": torch.sigmoid(self.confidence_proj(h)),
        }


class BrainCouncil(nn.Module):
    """
    BrainCouncil: four specialist recurrent blocks with governance overlay.

    Merge doctrine:
        advisor_delta = w_logic*logic + w_adv*adversary + w_verify*verifier_state
        advisor_delta = open_mask * advisor_delta
        mixed = seat + advisor_scale * advisor_delta

    When RoadGuard BLOCKED:
        advisor_delta = 0
        trace marks blocked
        Seat still emits safe/base prediction

    Cache isolation: each loop uses a unique cache_key to prevent KV contamination.
    """

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.dim = cfg.dim

        self.logic_loop = cfg.recurrent_block_cls(cfg)
        self.prob_loop = cfg.recurrent_block_cls(cfg)
        self.adversary_loop = cfg.recurrent_block_cls(cfg)
        self.verifier_loop = cfg.recurrent_block_cls(cfg)

        self.governor = GovernorRouter(cfg.dim)
        self.roadguard = RoadGuard(cfg.dim)
        self.verifier = VerifierHead(cfg.dim)

        self.role_embed = nn.Parameter(torch.randn(4, cfg.dim) * 0.02)
        self.council_read = nn.Linear(cfg.dim, cfg.dim, bias=False)

        # Sigmoid-constrained advisor scale: permanently in [0, 1]
        self._advisor_scale = nn.Parameter(torch.tensor(-0.85))  # sigmoid ≈ 0.30

        self.final_norm = RMSNorm(cfg.dim)

    @property
    def advisor_scale(self):
        return torch.sigmoid(self._advisor_scale)

    def forward(self, x, e, freqs_cis, mask=None, n_loops=None, kv_cache=None):
        B, T, D = x.shape

        council = self.council_read(x)

        # Each loop has a unique cache_key to prevent KV cache contamination
        logic = self.logic_loop(
            x + council, e + self.role_embed[0].view(1, 1, D),
            freqs_cis, mask, n_loops, kv_cache, cache_key="logic_loop"
        )
        seat = self.prob_loop(
            x + council, e + self.role_embed[1].view(1, 1, D),
            freqs_cis, mask, n_loops, kv_cache, cache_key="seat_loop"
        )
        adversary = self.adversary_loop(
            x + council, e + self.role_embed[2].view(1, 1, D),
            freqs_cis, mask, n_loops, kv_cache, cache_key="adversary_loop"
        )
        verifier_state = self.verifier_loop(
            x + council, e + self.role_embed[3].view(1, 1, D),
            freqs_cis, mask, n_loops, kv_cache, cache_key="verifier_loop"
        )

        gov = self.governor(x)
        guard_mask, guard_prob = self.roadguard(adversary)
        verifier = self.verifier(verifier_state)

        w = gov["weights"]
        w_logic = w[:, 0].view(-1, 1, 1)
        w_seat = w[:, 1].view(-1, 1, 1)
        w_adv = w[:, 2].view(-1, 1, 1)
        w_verify = w[:, 3].view(-1, 1, 1)

        # RoadGuard: 1.0 = BLOCKED, 0.0 = OPEN
        open_mask = 1.0 - guard_mask.view(-1, 1, 1)

        # Advisor delta: bounded modifier stream
        advisor_delta = (
            w_logic * logic
            + w_adv * adversary
            + w_verify * verifier_state
        )
        advisor_delta = open_mask * advisor_delta

        # Seat + bounded advisors (scale permanently in [0, 1] via sigmoid property)
        mixed = seat + self.advisor_scale * advisor_delta

        trace = {
            "governor_weights": gov["weights"],
            "governor_gates": gov["gates"],
            "governor_reason": gov["reason_latent"],
            "roadguard_block_mask": guard_mask,
            "roadguard_block_prob": guard_prob,
            "roadguard_blocked": (guard_mask > 0.5).float(),
            "verifier_scores": verifier["loop_scores"],
            "verifier_confidence": verifier["confidence"],
            "advisor_scale": self.advisor_scale.detach().clone(),
        }

        return self.final_norm(mixed), trace
