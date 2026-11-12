import os
from importlib.util import find_spec

# Hypothesis is not a project dependency yet: skip these tests without it.
collect_ignore_glob = [] if find_spec("hypothesis") else ["test_*.py"]

if find_spec("hypothesis"):
    from hypothesis import HealthCheck, settings

    # Default: the same examples on every run, nothing written to disk.
    settings.register_profile(
        "repeatable",
        max_examples=100,
        derandomize=True,
        database=None,
        deadline=None,
        suppress_health_check=list(HealthCheck),
    )
    # For hunting by hand: HYPOTHESIS_PROFILE=explore pytest tests/property
    settings.register_profile(
        "explore",
        max_examples=3000,
        deadline=None,
        suppress_health_check=list(HealthCheck),
    )
    settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "repeatable"))
