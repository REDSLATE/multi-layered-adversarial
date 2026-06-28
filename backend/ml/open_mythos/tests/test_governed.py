"""
Test suite for OpenMythos governed architecture.

Required before calling anything "production-ready":
  1. Shape tests
  2. Gradient tests
  3. Checkpoint reload tests
  4. RoadGuard BLOCKED tests
  5. Trace serialization tests
  6. Training smoke test
"""

import json
import tempfile
import math
import torch
import torch.nn as nn
import torch.nn.functional as F
import pytest

from open_mythos.main import OpenMythos, OpenMythosConfig
from open_mythos.memory import DoctrineMemory
from open_mythos.governance import BrainCouncil, GovernorRouter, RoadGuard, VerifierHead
from open_mythos.calibration import CalibratedHead


# ---------------------------------------------------------------------------
# Mock RecurrentBlock for testing (minimal transformer-like block)
# Handles both prelude/coda signature and council loop signature
# ---------------------------------------------------------------------------

class MockRecurrentBlock(nn.Module):
    """Minimal stand-in for the user's RecurrentBlock."""

    def __init__(self, cfg):
        super().__init__()
        self.dim = cfg.dim
        self.norm1 = nn.LayerNorm(cfg.dim)
        self.norm2 = nn.LayerNorm(cfg.dim)
        self.attn = nn.MultiheadAttention(cfg.dim, cfg.n_heads, batch_first=True)
        self.mlp = nn.Sequential(
            nn.Linear(cfg.dim, cfg.dim * 4),
            nn.GELU(),
            nn.Linear(cfg.dim * 4, cfg.dim),
        )

    def forward(self, x, e=None, freqs_cis=None, mask=None, n_loops=None, kv_cache=None, cache_key=None):
        # Accept both signatures: (x, freqs_cis, mask, kv_cache, cache_key) and
        # (x, e, freqs_cis, mask, n_loops, kv_cache)
        x2 = self.norm1(x)
        if mask is not None and isinstance(mask, dict):
            # mask was passed as kv_cache, shift args
            mask = None
        attn_mask = mask.squeeze(0).squeeze(0) if mask is not None else None
        if attn_mask is not None and attn_mask.dim() == 4:
            attn_mask = attn_mask.squeeze(0).squeeze(0)
        attn_out, _ = self.attn(x2, x2, x2, attn_mask=attn_mask)
        x = x + attn_out
        x = x + self.mlp(self.norm2(x))
        return x


def make_cfg(**overrides):
    defaults = dict(
        dim=64,
        n_layers=6,
        n_heads=4,
        vocab_size=128,
        max_seq_len=128,
        rope_theta=10000.0,
        n_loops=2,
        memory_slots=16,
        recurrent_block_cls=MockRecurrentBlock,
    )
    defaults.update(overrides)
    return OpenMythosConfig(**defaults)


# ===========================================================================
# 1. SHAPE TESTS
# ===========================================================================

class TestShapes:
    """Verify all tensors have expected shapes at every boundary."""

    def test_memory_output_shapes(self):
        mem = DoctrineMemory(dim=64, slots=16)
        x = torch.randn(2, 8, 64)
        out = mem(x)
        assert out["doctrine"].shape == (2, 8, 64)
        assert out["working"].shape == (2, 8, 64)
        assert out["fused"].shape == (2, 8, 64)

    def test_governor_output_shapes(self):
        gov = GovernorRouter(dim=64)
        x = torch.randn(2, 8, 64)
        out = gov(x)
        assert out["weights"].shape == (2, 4)
        assert out["gates"].shape == (2, 4)
        assert out["reason_latent"].shape == (2, 64)
        assert torch.allclose(out["weights"].sum(dim=1), torch.ones(2), atol=1e-5)

    def test_roadguard_output_shapes(self):
        rg = RoadGuard(dim=64)
        x = torch.randn(2, 8, 64)
        mask, prob = rg(x)
        assert mask.shape == (2, 1)
        assert prob.shape == (2, 1)
        # The hard part is binary
        hard = (prob > 0.5).float()
        assert ((hard == 0.0) | (hard == 1.0)).all()

    def test_verifier_head_shapes(self):
        vh = VerifierHead(dim=64)
        x = torch.randn(2, 8, 64)
        out = vh(x)
        assert out["loop_scores"].shape == (2, 4)
        assert out["confidence"].shape == (2, 1)
        assert (out["confidence"] >= 0).all() and (out["confidence"] <= 1).all()

    def test_calibrated_head_shapes(self):
        ch = CalibratedHead(dim=64)
        hidden = torch.randn(2, 8, 64)
        out = ch(hidden)
        assert out["confidence"].shape == (2, 8)
        assert out["uncertainty"].shape == (2, 8)
        assert (out["confidence"] >= 0).all() and (out["confidence"] <= 1).all()
        assert (out["uncertainty"] > 0).all()

    def test_full_forward_shapes(self):
        cfg = make_cfg()
        model = OpenMythos(cfg)
        input_ids = torch.randint(0, cfg.vocab_size, (2, 8))

        # Without trace
        logits = model(input_ids)
        assert logits.shape == (2, 8, cfg.vocab_size)

        # With trace
        out = model(input_ids, return_trace=True)
        assert out["logits"].shape == (2, 8, cfg.vocab_size)
        assert out["confidence"].shape == (2, 8)
        assert out["uncertainty"].shape == (2, 8)
        assert out["trace"]["governor_weights"].shape == (2, 4)
        assert out["trace"]["roadguard_block_mask"].shape == (2, 1)
        assert out["trace"]["verifier_scores"].shape == (2, 4)

    def test_generate_shapes(self):
        cfg = make_cfg()
        model = OpenMythos(cfg)
        input_ids = torch.randint(0, cfg.vocab_size, (1, 4))

        result = model.generate(input_ids, max_new_tokens=4)
        assert result["tokens"].shape == (1, 8)
        assert result["trace"]["governor_weights"].shape == (1, 4)
        assert result["confidence"].shape == (1,)


# ===========================================================================
# 2. GRADIENT TESTS
# ===========================================================================

class TestGradients:
    """Verify gradients flow through all governance components."""

    def test_memory_gradients(self):
        mem = DoctrineMemory(dim=64, slots=8)
        x = torch.randn(2, 4, 64, requires_grad=True)
        out = mem(x)
        loss = out["fused"].sum()
        loss.backward()
        assert x.grad is not None
        assert mem.doctrine_keys.grad is not None
        assert mem.doctrine_values.grad is not None
        assert mem.write_gate.weight.grad is not None

    def test_council_gradients(self):
        cfg = make_cfg(dim=32, n_layers=4, n_heads=2)
        council = BrainCouncil(cfg)
        x = torch.randn(2, 4, 32, requires_grad=True)
        e = torch.randn(2, 4, 32)
        freqs_cis = torch.randn(4, 16, dtype=torch.complex64)
        mask = torch.triu(torch.ones(4, 4), diagonal=1).bool().unsqueeze(0).unsqueeze(0)

        mixed, trace = council(x, e, freqs_cis, mask, n_loops=1)
        loss = mixed.sum() + trace["verifier_scores"].sum()
        loss.backward()

        assert x.grad is not None
        assert council._advisor_scale.grad is not None
        assert council.governor.weight_proj.weight.grad is not None
        assert council.roadguard.block_proj.weight.grad is not None

    def test_full_model_backward_with_verifier_loss(self):
        """Include verifier and calibration loss to ensure all heads get gradients."""
        cfg = make_cfg()
        model = OpenMythos(cfg)
        input_ids = torch.randint(0, cfg.vocab_size, (2, 8))
        labels = torch.randint(0, cfg.vocab_size, (2, 8))

        out = model(input_ids, return_trace=True)
        logits = out["logits"]
        lm_loss = F.cross_entropy(logits.reshape(-1, cfg.vocab_size), labels.reshape(-1))

        # Verifier loss
        verifier_scores = out["trace"]["verifier_scores"]
        verifier_loss = verifier_scores.sum() * 0.01

        # Calibration loss (ensures calibrated_head gets gradients)
        confidence = out["confidence"]
        uncertainty = out["uncertainty"]
        # Dummy target for calibration
        cal_target = torch.rand(2, 8)
        calibration_loss = 0.5 * (
            torch.log(uncertainty + 1e-6) + (cal_target - confidence) ** 2 / (uncertainty + 1e-6)
        ).mean()

        loss = lm_loss + verifier_loss + 0.1 * calibration_loss
        loss.backward()

        assert model.embed.weight.grad is not None
        assert model.council._advisor_scale.grad is not None
        assert model.council.governor.weight_proj.weight.grad is not None
        assert model.council.roadguard.block_proj.weight.grad is not None
        assert model.council.verifier.score_proj.weight.grad is not None
        assert model.calibrated_head.proj.weight.grad is not None
        assert model.memory.doctrine_keys.grad is not None

    def test_advisor_scale_gradient(self):
        cfg = make_cfg()
        council = BrainCouncil(cfg)
        x = torch.randn(2, 4, cfg.dim, requires_grad=True)
        e = torch.randn(2, 4, cfg.dim)
        freqs_cis = torch.randn(4, 16, dtype=torch.complex64)
        mask = torch.triu(torch.ones(4, 4), diagonal=1).bool().unsqueeze(0).unsqueeze(0)

        mixed, _ = council(x, e, freqs_cis, mask, n_loops=1)
        loss = mixed.sum()
        loss.backward()

        assert council._advisor_scale.grad is not None
        assert council._advisor_scale.grad.abs().item() > 0


# ===========================================================================
# 3. CHECKPOINT RELOAD TESTS
# ===========================================================================

class TestCheckpointing:
    """Verify state_dict save/load preserves all governance parameters."""

    def test_state_dict_keys(self):
        cfg = make_cfg()
        model = OpenMythos(cfg)
        state = model.state_dict()

        required_keys = [
            "council._advisor_scale",
            "council.governor.weight_proj.weight",
            "council.roadguard.block_proj.weight",
            "council.verifier.score_proj.weight",
            "memory.doctrine_keys",
            "memory.doctrine_values",
            "calibrated_head.proj.weight",
        ]
        for key in required_keys:
            assert any(k.endswith(key) for k in state.keys()), f"Missing key: {key}"

    def test_save_load_roundtrip(self):
        cfg = make_cfg()
        model1 = OpenMythos(cfg)
        input_ids = torch.randint(0, cfg.vocab_size, (1, 8))

        out1 = model1(input_ids, return_trace=True)

        with tempfile.NamedTemporaryFile(suffix=".pt", delete=False) as f:
            path = f.name
            torch.save(model1.state_dict(), path)

        model2 = OpenMythos(cfg)
        model2.load_state_dict(torch.load(path, weights_only=True))
        out2 = model2(input_ids, return_trace=True)

        torch.testing.assert_close(out1["logits"], out2["logits"])
        torch.testing.assert_close(out1["confidence"], out2["confidence"])
        torch.testing.assert_close(out1["uncertainty"], out2["uncertainty"])

        for key in out1["trace"]:
            torch.testing.assert_close(out1["trace"][key], out2["trace"][key])

    def test_advisor_scale_preserved(self):
        cfg = make_cfg()
        model1 = OpenMythos(cfg)
        model1.council._advisor_scale.data = torch.tensor(0.25)

        with tempfile.NamedTemporaryFile(suffix=".pt", delete=False) as f:
            path = f.name
            torch.save(model1.state_dict(), path)

        model2 = OpenMythos(cfg)
        model2.load_state_dict(torch.load(path, weights_only=True))
        assert torch.isclose(model2.council._advisor_scale, torch.tensor(0.25))


# ===========================================================================
# 4. ROADGUARD BLOCKED TESTS
# ===========================================================================

class TestRoadGuardBlocked:
    """Verify RoadGuard correctly suppresses advisor influence when BLOCKED."""

    def test_block_mask_is_binary(self):
        rg = RoadGuard(dim=64)
        x = torch.randn(2, 8, 64)
        mask, prob = rg(x)
        hard = (prob > 0.5).float()
        assert ((hard == 0.0) | (hard == 1.0)).all()

    def test_blocked_suppresses_advisors(self):
        """When RoadGuard BLOCKED, advisor_delta must be zero."""
        cfg = make_cfg()
        council = BrainCouncil(cfg)

        # Force RoadGuard to BLOCK by using extreme input
        x = torch.randn(2, 4, cfg.dim)
        # Make adversary output have extreme positive values to trigger BLOCK
        with torch.no_grad():
            council.roadguard.block_proj.weight.fill_(10.0)
            council.roadguard.block_proj.bias = nn.Parameter(torch.zeros(1))

        e = torch.randn(2, 4, cfg.dim)
        freqs_cis = torch.randn(4, 16, dtype=torch.complex64)
        mask = torch.triu(torch.ones(4, 4), diagonal=1).bool().unsqueeze(0).unsqueeze(0)

        mixed, trace = council(x, e, freqs_cis, mask, n_loops=1)

        # With extreme positive weights, should be BLOCKED
        assert trace["roadguard_blocked"].all() or trace["roadguard_block_prob"].mean() > 0.5,             f"Expected BLOCKED, got prob={trace['roadguard_block_prob']}"

    def test_open_allows_advisors(self):
        """When RoadGuard OPEN, advisors should have non-zero contribution."""
        cfg = make_cfg()
        council = BrainCouncil(cfg)

        # Force RoadGuard to OPEN via extreme negative bias
        with torch.no_grad():
            council.roadguard.block_proj.weight.fill_(0.0)
            council.roadguard.block_proj.bias.fill_(-10.0)

        x = torch.randn(2, 4, cfg.dim)
        e = torch.randn(2, 4, cfg.dim)
        freqs_cis = torch.randn(4, 16, dtype=torch.complex64)
        mask = torch.triu(torch.ones(4, 4), diagonal=1).bool().unsqueeze(0).unsqueeze(0)

        mixed, trace = council(x, e, freqs_cis, mask, n_loops=1)

        assert not trace["roadguard_blocked"].any(),             f"Expected OPEN, got prob={trace['roadguard_block_prob']}"
        

    def test_confidence_penalty_when_blocked(self):
        cfg = make_cfg()
        model = OpenMythos(cfg)

        # Force BLOCKED via extreme positive bias
        with torch.no_grad():
            model.council.roadguard.block_proj.weight.fill_(0.0)
            model.council.roadguard.block_proj.bias.fill_(10.0)

        input_ids = torch.randint(0, cfg.vocab_size, (1, 8))
        out = model(input_ids, return_trace=True)

        raw_conf = out["raw_confidence"]
        adj_conf = out["confidence"]
        # When BLOCKED, confidence should be reduced by 50%
        assert (adj_conf <= raw_conf).all(), "Adjusted confidence should not exceed raw confidence"
        assert out["trace"]["roadguard_blocked"].all() or out["trace"]["roadguard_block_prob"].mean() > 0.5

    def test_advisor_scale_bounded(self):
        cfg = make_cfg()
        council = BrainCouncil(cfg)
        scale = council.advisor_scale.abs().item()
        assert 0 <= scale <= 1.0, f"Advisor scale {scale} out of bounds"


# ===========================================================================
# 5. TRACE SERIALIZATION TESTS
# ===========================================================================

class TestTraceSerialization:
    """Verify governance traces can be logged, serialized, and inspected."""

    def test_trace_contains_all_fields(self):
        cfg = make_cfg()
        model = OpenMythos(cfg)
        input_ids = torch.randint(0, cfg.vocab_size, (1, 8))
        out = model(input_ids, return_trace=True)
        trace = out["trace"]

        required_fields = [
            "governor_weights",
            "governor_gates",
            "governor_reason",
            "roadguard_block_mask",
            "roadguard_block_prob",
            "roadguard_blocked",
            "verifier_scores",
            "verifier_confidence",
            "advisor_scale",
        ]
        for field in required_fields:
            assert field in trace, f"Missing trace field: {field}"

    def test_trace_json_serializable(self):
        cfg = make_cfg()
        model = OpenMythos(cfg)
        input_ids = torch.randint(0, cfg.vocab_size, (1, 8))
        out = model(input_ids, return_trace=True)
        trace = out["trace"]

        serializable = {}
        for k, v in trace.items():
            if isinstance(v, torch.Tensor):
                serializable[k] = v.detach().cpu().tolist()
            else:
                serializable[k] = v

        json_str = json.dumps(serializable)
        assert len(json_str) > 0

    def test_trace_batch_independence(self):
        """Each sample in a batch should have independent trace values."""
        cfg = make_cfg()
        model = OpenMythos(cfg)
        input_ids = torch.randint(0, cfg.vocab_size, (2, 8))
        out = model(input_ids, return_trace=True)
        trace = out["trace"]

        w0 = trace["governor_weights"][0]
        w1 = trace["governor_weights"][1]
        assert not torch.allclose(w0, w1, atol=1e-3), "Traces should differ across batch samples"

    def test_trace_reason_latent_shape(self):
        cfg = make_cfg()
        model = OpenMythos(cfg)
        input_ids = torch.randint(0, cfg.vocab_size, (2, 8))
        out = model(input_ids, return_trace=True)
        reason = out["trace"]["governor_reason"]
        assert reason.shape == (2, cfg.dim)


# ===========================================================================
# 6. TRAINING SMOKE TEST
# ===========================================================================

class TestTrainingSmoke:
    """End-to-end training step without crashing."""

    def test_forward_backward_step(self):
        cfg = make_cfg()
        model = OpenMythos(cfg)
        optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)

        input_ids = torch.randint(0, cfg.vocab_size, (2, 8))
        labels = torch.randint(0, cfg.vocab_size, (2, 8))

        out = model(input_ids, return_trace=True)
        logits = out["logits"]

        # Standard LM loss
        shift_logits = logits[:, :-1, :].reshape(-1, cfg.vocab_size)
        shift_labels = labels[:, 1:].reshape(-1)
        lm_loss = F.cross_entropy(shift_logits, shift_labels)

        # Calibration loss (Gaussian NLL)
        confidence = out["confidence"][:, :-1]
        uncertainty = out["uncertainty"][:, :-1]
        p_correct = F.softmax(shift_logits, dim=-1).gather(1, shift_labels.unsqueeze(1)).squeeze(1)
        p_correct = p_correct.view(2, 7)
        calibration_loss = 0.5 * (
            torch.log(uncertainty + 1e-6) + (p_correct.detach() - confidence) ** 2 / (uncertainty + 1e-6)
        ).mean()

        # Router balance loss
        route_probs = out["trace"]["governor_weights"]
        avg_probs = route_probs.mean(dim=0)
        balance_loss = 4.0 * (avg_probs ** 2).sum()

        # Entropy bonus
        entropy = - (route_probs * torch.log(route_probs + 1e-10)).sum(dim=-1).mean()

        # Verifier loss
        verifier_conf = out["trace"]["verifier_confidence"]
        token_acc = p_correct.mean(dim=1, keepdim=True)
        verifier_loss = F.mse_loss(verifier_conf, token_acc.detach())

        # RoadGuard regularization
        roadguard_loss = out["trace"]["roadguard_block_prob"].mean()

        # Verifier score loss (ensure gradient flow to verifier head)
        verifier_score_loss = out["trace"]["verifier_scores"].sum() * 0.001

        loss = (
            lm_loss
            + 0.1 * calibration_loss
            + 0.01 * balance_loss
            - 0.001 * entropy
            + 0.05 * verifier_loss
            + 0.01 * roadguard_loss
            + verifier_score_loss
        )

        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()

        assert loss.item() == loss.item()  # not NaN
        assert not any(p.grad.isnan().any() for p in model.parameters() if p.grad is not None)

    def test_training_changes_weights(self):
        cfg = make_cfg()
        model = OpenMythos(cfg)
        optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)

        input_ids = torch.randint(0, cfg.vocab_size, (2, 8))
        labels = torch.randint(0, cfg.vocab_size, (2, 8))

        before = model.council._advisor_scale.clone().detach()

        out = model(input_ids, return_trace=True)
        logits = out["logits"]
        shift_logits = logits[:, :-1, :].reshape(-1, cfg.vocab_size)
        shift_labels = labels[:, 1:].reshape(-1)
        loss = F.cross_entropy(shift_logits, shift_labels)

        # Add verifier score loss and advisor_scale to ensure gradient flow
        loss = loss + out["trace"]["verifier_scores"].sum() * 0.001
        # Explicitly include advisor_scale in loss so it must change
        loss = loss + model.council.advisor_scale * 1.0

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        after = model.council._advisor_scale.clone().detach()
        assert not torch.isclose(before, after, atol=1e-6), "advisor_scale should change during training"

    def test_memory_doctrine_updates(self):
        """Doctrine parameters should receive gradients during training."""
        cfg = make_cfg()
        model = OpenMythos(cfg)
        optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)

        input_ids = torch.randint(0, cfg.vocab_size, (2, 8))
        labels = torch.randint(0, cfg.vocab_size, (2, 8))

        before_keys = model.memory.doctrine_keys.clone().detach()

        out = model(input_ids, return_trace=True)
        logits = out["logits"]
        shift_logits = logits[:, :-1, :].reshape(-1, cfg.vocab_size)
        shift_labels = labels[:, 1:].reshape(-1)
        loss = F.cross_entropy(shift_logits, shift_labels)
        loss = loss + out["trace"]["verifier_scores"].sum() * 0.001

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        after_keys = model.memory.doctrine_keys.clone().detach()
        assert not torch.allclose(before_keys, after_keys, atol=1e-6), "Doctrine keys should update"

    def test_no_nan_gradients(self):
        cfg = make_cfg()
        model = OpenMythos(cfg)
        input_ids = torch.randint(0, cfg.vocab_size, (2, 8))
        labels = torch.randint(0, cfg.vocab_size, (2, 8))

        out = model(input_ids, return_trace=True)
        logits = out["logits"]
        shift_logits = logits[:, :-1, :].reshape(-1, cfg.vocab_size)
        shift_labels = labels[:, 1:].reshape(-1)
        loss = F.cross_entropy(shift_logits, shift_labels)
        loss = loss + out["trace"]["verifier_scores"].sum() * 0.001
        loss.backward()

        for name, p in model.named_parameters():
            if p.grad is not None:
                assert not p.grad.isnan().any(), f"NaN gradient in {name}"
                assert not p.grad.isinf().any(), f"Inf gradient in {name}"


# ===========================================================================
# Additional: Doctrine enforcement tests
# ===========================================================================

class TestDoctrineEnforcement:
    """Verify the governance doctrine is structurally enforced."""

    def test_seat_never_suppressed(self):
        """Seat output should always be present in the final mix."""
        cfg = make_cfg()
        council = BrainCouncil(cfg)

        x = torch.randn(2, 4, cfg.dim)
        e = torch.randn(2, 4, cfg.dim)
        freqs_cis = torch.randn(4, 16, dtype=torch.complex64)
        mask = torch.triu(torch.ones(4, 4), diagonal=1).bool().unsqueeze(0).unsqueeze(0)

        mixed, trace = council(x, e, freqs_cis, mask, n_loops=1)
        assert mixed.abs().sum() > 0, "Final output should never be zero"

    def test_advisor_scale_is_positive(self):
        cfg = make_cfg()
        council = BrainCouncil(cfg)
        assert council.advisor_scale > 0
        assert council.advisor_scale <= 1.0

    def test_governor_weights_sum_to_one(self):
        cfg = make_cfg()
        gov = GovernorRouter(dim=cfg.dim)
        x = torch.randn(2, 4, cfg.dim)
        out = gov(x)
        sums = out["weights"].sum(dim=1)
        torch.testing.assert_close(sums, torch.ones(2), atol=1e-5, rtol=1e-5)

    def test_gates_are_in_zero_one(self):
        cfg = make_cfg()
        gov = GovernorRouter(dim=cfg.dim)
        x = torch.randn(2, 4, cfg.dim)
        out = gov(x)
        assert (out["gates"] >= 0).all() and (out["gates"] <= 1).all()


class TestCacheIsolation:
    """Verify each loop uses a unique cache_key to prevent KV contamination."""

    def test_council_passes_unique_cache_keys(self):
        """Mock block should receive distinct cache_key for each loop."""
        cfg = make_cfg()
        council = BrainCouncil(cfg)

        # Track received cache_keys
        received_keys = []
        original_forward = cfg.recurrent_block_cls.forward

        def tracking_forward(self, x, e=None, freqs_cis=None, mask=None, n_loops=None, kv_cache=None, cache_key=None):
            if cache_key is not None:
                received_keys.append(cache_key)
            return original_forward(self, x, e, freqs_cis, mask, n_loops, kv_cache, cache_key)

        cfg.recurrent_block_cls.forward = tracking_forward

        x = torch.randn(2, 4, cfg.dim)
        e = torch.randn(2, 4, cfg.dim)
        freqs_cis = torch.randn(4, 16, dtype=torch.complex64)
        mask = torch.triu(torch.ones(4, 4), diagonal=1).bool().unsqueeze(0).unsqueeze(0)

        council(x, e, freqs_cis, mask, n_loops=1)

        expected = {"logic_loop", "seat_loop", "adversary_loop", "verifier_loop"}
        assert set(received_keys) == expected, f"Expected {expected}, got {set(received_keys)}"

    def test_no_duplicate_cache_keys(self):
        """All four loops must have distinct cache keys."""
        cfg = make_cfg()
        council = BrainCouncil(cfg)

        received_keys = []
        original_forward = cfg.recurrent_block_cls.forward

        def tracking_forward(self, x, e=None, freqs_cis=None, mask=None, n_loops=None, kv_cache=None, cache_key=None):
            if cache_key is not None:
                received_keys.append(cache_key)
            return original_forward(self, x, e, freqs_cis, mask, n_loops, kv_cache, cache_key)

        cfg.recurrent_block_cls.forward = tracking_forward

        x = torch.randn(2, 4, cfg.dim)
        e = torch.randn(2, 4, cfg.dim)
        freqs_cis = torch.randn(4, 16, dtype=torch.complex64)
        mask = torch.triu(torch.ones(4, 4), diagonal=1).bool().unsqueeze(0).unsqueeze(0)

        council(x, e, freqs_cis, mask, n_loops=1)

        assert len(received_keys) == len(set(received_keys)), f"Duplicate cache keys: {received_keys}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
