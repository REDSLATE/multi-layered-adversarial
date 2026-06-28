"""
Training script for OpenMythos with governance-aware multi-task loss.

Loss composition:
    1. LM loss (next-token cross-entropy)
    2. Calibration loss (Gaussian NLL on soft correctness)
    3. Router load balancing (prevent council collapse)
    4. Router entropy bonus (encourage decisive but diverse routing)
    5. Verifier P&L credit assignment (REINFORCE-style loop scoring)
    6. RoadGuard binary loss (adversarial detection)

Curriculum:
    Phase 1: Brain reliability (freeze council except logic_loop)
    Phase 2: Verifier backlog (train verifier scoring)
    Phase 3: Seat + Governor (execution training)
    Phase 4: RoadGuard (adversarial hardening)
    Phase 5: Full council (all losses active)
"""

import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader

from open_mythos.main import OpenMythos, OpenMythosConfig


def compute_soft_correctness(logits: torch.Tensor, labels: torch.Tensor):
    """
    Compute per-token soft correctness: model's probability on the true token.

    Args:
        logits: [B, T, V]
        labels: [B, T] — target token ids
    Returns:
        p_correct: [B, T] — probability assigned to true token
    """
    probs = F.softmax(logits, dim=-1)  # [B, T, V]
    p_correct = probs.gather(-1, labels.unsqueeze(-1)).squeeze(-1)  # [B, T]
    return p_correct


def council_loss(trace: dict, labels: torch.Tensor, logits: torch.Tensor):
    """
    Multi-task governance loss.

    Returns:
        dict of scalar losses
    """
    # 1. Calibration loss (Gaussian NLL)
    # We need p_correct as the target for calibration
    p_correct = compute_soft_correctness(logits, labels)  # [B, T]

    # The calibrated head outputs are in trace via forward(), but we need
    # to recompute or pass them. In practice, the model returns them.
    # Here we compute auxiliary losses from trace.

    route_probs = trace["governor_weights"]  # [B, 4]

    # 2. Load balancing: want uniform distribution across loops
    avg_probs = route_probs.mean(dim=0)  # [4]
    balance_loss = 4.0 * (avg_probs ** 2).sum()  # min when uniform

    # 3. Entropy bonus: encourage decisive but diverse routing
    entropy = - (route_probs * torch.log(route_probs + 1e-10)).sum(dim=-1).mean()
    entropy_bonus = -entropy  # negative because we maximize entropy

    # 4. Verifier P&L: verifier scores should correlate with loop utility
    # Simplified: encourage verifier confidence to match actual accuracy trend
    verifier_conf = trace["verifier_confidence"]  # [B, 1]
    # Target: verifier should be confident when model is correct
    token_acc = p_correct.mean(dim=1, keepdim=True)  # [B, 1]
    verifier_loss = F.mse_loss(verifier_conf, token_acc.detach())

    # 5. RoadGuard: on easy/clean data, block_prob should be low (OPEN)
    # On adversarial data (not shown here), it should be high (BLOCKED)
    # For standard training, we regularize toward OPEN
    roadguard_prob = trace["roadguard_block_prob"]  # [B, 1]
    roadguard_loss = roadguard_prob.mean()  # regularize to 0 (OPEN)

    return {
        "balance": balance_loss,
        "entropy": entropy_bonus,
        "verifier": verifier_loss,
        "roadguard": roadguard_loss,
    }


def train_step(model: OpenMythos, batch: dict, optimizer: torch.optim.Optimizer, step: int):
    """Single training step with full governance loss."""
    model.train()
    input_ids = batch["input_ids"]  # [B, T]
    labels = batch["labels"]        # [B, T]

    optimizer.zero_grad()

    # Forward with governance trace
    out = model(input_ids, return_trace=True)

    logits = out["logits"]          # [B, T, V]
    confidence = out["confidence"]  # [B, T]
    uncertainty = out["uncertainty"]  # [B, T]
    trace = out["trace"]

    # 1. LM loss (next-token prediction)
    shift_logits = logits[:, :-1, :].reshape(-1, logits.size(-1))
    shift_labels = labels[:, 1:].reshape(-1)
    lm_loss = F.cross_entropy(shift_logits, shift_labels)

    # 2. Calibration loss (Gaussian NLL)
    # Target: soft correctness on shifted labels
    shift_p_correct = compute_soft_correctness(logits[:, :-1, :], labels[:, 1:])
    precision = 1.0 / (uncertainty[:, :-1] + 1e-6)
    calibration_loss = 0.5 * (
        torch.log(uncertainty[:, :-1]) + (shift_p_correct.detach() - confidence[:, :-1]) ** 2 / uncertainty[:, :-1]
    ).mean()

    # 3. Council auxiliary losses
    aux = council_loss(trace, labels[:, 1:], logits[:, :-1, :])

    # Compose total loss with curriculum weights
    # Phase 5: all active
    loss = (
        lm_loss
        + 0.1 * calibration_loss
        + 0.01 * aux["balance"]
        - 0.001 * aux["entropy"]
        + 0.05 * aux["verifier"]
        + 0.01 * aux["roadguard"]
    )

    loss.backward()
    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
    optimizer.step()

    return {
        "loss": loss.item(),
        "lm_loss": lm_loss.item(),
        "calibration": calibration_loss.item(),
        "balance": aux["balance"].item(),
        "verifier": aux["verifier"].item(),
        "roadguard": aux["roadguard"].item(),
        "avg_confidence": confidence.mean().item(),
        "avg_uncertainty": uncertainty.mean().item(),
        "roadguard_block_rate": trace["roadguard_block_mask"].mean().item(),
        "governor_weights": trace["governor_weights"].mean(dim=0).detach().cpu().tolist(),
    }


def main():
    """Example training loop."""
    # NOTE: You must provide a RecurrentBlock implementation.
    # Example:
    # from your_module import RecurrentBlock
    # cfg = OpenMythosConfig(dim=1024, n_layers=24, recurrent_block_cls=RecurrentBlock)

    print("OpenMythos training script loaded.")
    print("Inject your RecurrentBlock into OpenMythosConfig.recurrent_block_cls")
    print("Then instantiate: model = OpenMythos(cfg)")
    print("And call: train_step(model, batch, optimizer, step)")


if __name__ == "__main__":
    main()
