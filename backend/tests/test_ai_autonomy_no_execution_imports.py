"""Authority pin: this directory must NEVER import the execution path,
RoadGuard, or a broker adapter. The whole point of the side-band is
that a self-trained model cannot escape into a live order. If this
test fails, the merge is wrong — don't relax the assertion, fix the
import."""
from pathlib import Path


FORBIDDEN = [
    "shared.execution",
    "shared.broker_router",
    "roadguard",
    "alpaca",
    "kraken",
    "submit_order",
    "place_order",
]


def test_ai_autonomy_has_no_execution_imports():
    root = Path(__file__).resolve().parent.parent / "shared" / "ai_autonomy"
    text = "\n".join(
        p.read_text() for p in root.glob("*.py") if p.name != "__init__.py"
    )

    for item in FORBIDDEN:
        assert item.lower() not in text.lower(), (
            f"ai_autonomy must not import or reference {item!r}; "
            "this is the authority firewall"
        )
