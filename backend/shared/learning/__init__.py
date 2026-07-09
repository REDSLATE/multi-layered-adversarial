"""Live learning loop — turns every real order (fill OR reject) into
training material for RISE.

Doctrine (2026-07-09 operator directive):

    "Paper mode teaches structure. It does not teach slippage,
     broker rejects, spread behavior, fear points, missed fills,
     or real market timing. Learning requires exposure."

    Pipeline shape:

        real market intent
          → live micro/toehold execution
          → broker fill / reject
          → outcome resolver
          → experience store       ← this module
          → bucket analyzer        (Stage 2, deferred)
          → lesson proposal        (Stage 2, deferred)
          → Kernel review          (Stage 3, deferred)
          → doctrine update

    Stage 1 (this build): live capture + outcome resolver + admin
    read surface. Everything downstream reads the
    `learning_experiences` collection this stage populates.
"""
