"""Pytest configuration for benchmarks.

Benchmarks are only run with --benchmark-only flag.
This conftest marks all benchmark tests appropriately.
"""

import pytest


def pytest_collection_modifyitems(items):
    """Mark all benchmark tests and register benchmarks marker."""
    for item in items:
        if "bench_" in item.nodeid:
            item.add_marker(pytest.mark.benchmark)
