"""Pytest configuration for E2E tests."""


def pytest_addoption(parser):
    """Add custom command-line options for E2E tests."""
    parser.addoption(
        "--dwh-report",
        action="store_true",
        default=False,
        help="Generate DWH parse quality report (docs/DWH_PARSE_REPORT.md)",
    )
