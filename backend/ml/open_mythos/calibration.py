"""
CalibratedHead — Gaussian parameterization of correctness.

Outputs confidence (μ) and uncertainty (σ²) as a coupled pair,
not two independent sigmoids. Uses Gaussian NLL for training.
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


class CalibratedHead(nn.Module):
    """
    Single head parameterizing a Gaussian over per-token correctness.

    confidence = μ (mean)
    uncertainty = σ² (variance)

    Training target: model's own probability on the true token (soft correctness).
    """

    def __init__(self, dim: int):
        super().__init__()
        self.norm = RMSNorm(dim)
        # Output [mean, log_precision]
        self.proj = nn.Linear(dim, 2, bias=False)

    def forward(self, hidden: torch.Tensor):
        """
        Args:
            hidden: [B, T, D] — final hidden states before logits
        Returns:
            dict with confidence [B, T], uncertainty [B, T]
        """
        h = self.norm(hidden)  # [B, T, D]
        p = self.proj(h)       # [B, T, 2]

        mu = torch.sigmoid(p[..., 0])       # confidence in [0, 1]
        precision = F.softplus(p[..., 1]) + 1e-6
        sigma = 1.0 / precision              # uncertainty

        return {
            "confidence": mu,      # [B, T]
            "uncertainty": sigma,  # [B, T]
        }

    def loss(self, cal_out: dict, p_correct: torch.Tensor):
        """
        Gaussian Negative Log-Likelihood calibration loss.

        Args:
            cal_out: output from forward()
            p_correct: [B, T] — soft correctness target (model's prob on true token)
        Returns:
            scalar loss
        """
        mu = cal_out["confidence"]
        sigma = cal_out["uncertainty"]

        # Gaussian NLL: 0.5 * (log(sigma) + (target - mu)^2 / sigma)
        nll = 0.5 * (torch.log(sigma) + (p_correct.detach() - mu) ** 2 / sigma)
        return nll.mean()
