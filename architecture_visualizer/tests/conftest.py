"""pytest configuration for architecture_visualizer tests.

We need the ``importlib`` import mode so a test file that pulls in
``ternary_matmul/sw_utils/lib/config.py`` doesn't collide with
``architecture_visualizer/lib/`` (both would otherwise register as the
top-level ``lib`` package). importlib mode loads each test module under
its full dotted name, leaving import-resolution unaffected by pytest's
sys.path injection.
"""

collect_ignore_glob: list[str] = []


def pytest_configure(config):
    # Register the ``slow`` marker so @pytest.mark.slow doesn't raise
    # PytestUnknownMarkWarning.
    config.addinivalue_line(
        "markers",
        "slow: marks tests that take >1s (subprocess calls, big model loads)",
    )
