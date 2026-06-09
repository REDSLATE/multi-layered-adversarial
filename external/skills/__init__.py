"""Skills layer for the in-process brains.

See `loader.py` and `selector.py` for the public API. Skills are
markdown documents under `skill_pack/<name>/SKILL.md`. Each has YAML
frontmatter (`name`, `description`, `tags`) and a free-form body.

Doctrine: skills SHAPE hypotheses (which path to take, which guards
to apply, confidence biasing). They never AUTHORIZE execution. The
brain runner always finishes by POSTing the intent to MC's loopback,
where the existing gates (lane toggle, ladder stage, sizing_gate,
exposure caps, MC receipt) decide whether anything reaches a broker.
"""
