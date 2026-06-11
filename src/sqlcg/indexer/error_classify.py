"""Error classification for lineage extraction.

Maps structured error messages recorded during parsing into E-code buckets
for measurement and summary reporting.
"""

import json

# Priority order when one file emits multiple distinct buckets.
# Highest blast-radius / most severe failures first; pure_ddl_skip last
# (non-degrading: a deliberate skip, not a failure).
_CAUSE_PRIORITY: list[str] = [
    "timeout",
    "worker_error",
    "E8",
    "E3",
    "E2",
    "E5",
    "E1",
    "qualify_failed",
    "func_fallback",
    "pure_ddl_skip",
]

# Buckets that represent a genuine parse degradation (as opposed to a
# deliberate skip or fully unclassifiable noise).
_DEGRADING: frozenset[str] = frozenset(
    ["E1", "E2", "E3", "E5", "E8", "timeout", "worker_error", "func_fallback", "qualify_failed"]
)


def dominant_cause(errors: list[str]) -> tuple[str, bool]:
    """Return (parse_cause, parse_failed) for one file's error list.

    Reuses _classify_error per message, counts buckets, and returns the most
    frequent non-"other" bucket. Ties are broken by _CAUSE_PRIORITY (highest
    severity wins). Returns ("", False) when the list is empty or every message
    classifies as "other".

    Args:
        errors: List of structured error/skip strings from ParsedFile.errors.

    Returns:
        (parse_cause, parse_failed) where parse_cause is the dominant E-code
        bucket string (or "" when clean) and parse_failed is True when the
        dominant cause is in _DEGRADING.
    """
    if not errors:
        return ("", False)

    counts: dict[str, int] = {}
    for msg in errors:
        bucket = _classify_error(msg)
        if bucket != "other":
            counts[bucket] = counts.get(bucket, 0) + 1

    if not counts:
        return ("", False)

    # Find the maximum count
    max_count = max(counts.values())
    # Candidates are all buckets tied at the maximum count
    candidates = [b for b, c in counts.items() if c == max_count]

    # Break ties using _CAUSE_PRIORITY (lowest index = highest priority)
    priority_index: dict[str, int] = {b: i for i, b in enumerate(_CAUSE_PRIORITY)}
    # Buckets not in _CAUSE_PRIORITY get a sentinel high index
    sentinel = len(_CAUSE_PRIORITY)
    candidates.sort(key=lambda b: priority_index.get(b, sentinel))

    cause = candidates[0]
    return (cause, cause in _DEGRADING)


def _classify_error(msg: str) -> str:
    """Map a structured error message to its E-code or skip-reason bucket.

    Buckets (defined in ARCHITECTURE_REVIEW.md § 12.3 + sprint 09 plan):
      - "E1": col_lineage:NULL:Cannot find column 'NULL' (driven to 0 by sprint 08 T-05)
      - "E2": col_lineage:...:Cannot find column '...' with '(' or non-identifier chars
      - "E3": col_lineage:...:Expecting / Invalid expression / Unexpected token
      - "E5": col_lineage:...:Cannot find column (plain identifier)
      - "E8": col_lineage_skip:dynamic_source
      - "timeout": timeout:Ns
      - "worker_error": worker_error:* (pool worker returned an exception object)
      - "pure_ddl_skip": col_lineage_skip:pure_ddl_file
      - "func_fallback": col_lineage_skip:func_fallback:*
      - "qualify_failed": col_lineage_skip:qualify_failed:*
      - "other": anything else

    Args:
        msg: Structured error message from ParsedFile.errors or QueryNode.column_lineage errors.

    Returns:
        Bucket name (one of the 10 defined strings above, or "other").
    """
    if not msg:
        return "other"

    # Timeout errors (including pool-path poison retries)
    if msg.startswith("timeout:"):
        return "timeout"

    # Poison-retry: file repeatedly timed out in pool worker; treat as timeout bucket
    if msg.startswith("skipped:poison"):
        return "timeout"

    # Worker-level exception returned via pool (e.g. _error_file / pipe errors)
    # Format: "worker_error:<ExcType>:<message>" or "worker_error:send_failed"
    if msg.startswith("worker_error:"):
        return "worker_error"

    # Skip markers
    if msg.startswith("col_lineage_skip:"):
        if "pure_ddl_file" in msg:
            return "pure_ddl_skip"
        if "func_fallback:" in msg:
            return "func_fallback"
        if "qualify_failed:" in msg:
            return "qualify_failed"
        if "dynamic_source" in msg:
            return "E8"

    # Column lineage extraction errors (E1/E2/E3/E5)
    if msg.startswith("col_lineage:"):
        # Parse the error message to find the actual error type
        # Format: col_lineage:<col>:<error_message>
        if "Cannot find column 'NULL'" in msg:
            return "E1"
        if "Cannot find column" in msg:
            # Check if the column name has special characters (E2) or is plain (E5)
            # E2 examples: "YEAR(...)", "DATE(...)", etc. have parens or special chars
            # E5 examples: "ROTATIE", plain identifiers
            # Look for the quoted column name after "Cannot find column '"
            if "'" in msg:
                # Extract the column name between quotes
                import re

                match = re.search(r"Cannot find column '([^']+)'", msg)
                if match:
                    col_name = match.group(1)
                    # If it contains parentheses or special function syntax, it's E2
                    if "(" in col_name or ")" in col_name:
                        return "E2"
                    # Otherwise it's E5
                    return "E5"
            # Fallback for parsing errors
            if "Expecting" in msg or "Invalid" in msg or "Unexpected" in msg:
                return "E3"
            # Default to E5 for column-not-found
            return "E5"

    return "other"


def skip_counts_json(errors: list[str]) -> str | None:
    """Return a JSON-encoded {reason: count} map of col_lineage_skip:* reasons.

    Groups ``col_lineage_skip:<reason>:<detail>`` strings by their reason prefix
    (the segment between the first and second colon after 'col_lineage_skip:').
    Other error strings are ignored.  Returns None when no skip entries are found
    (the column stores NULL for clean files, which is cheaper than '{}').

    Designed for persistence on the File node as the ``skip_counts`` column so
    that §G accounting (converted vs dropped) is queryable via ``run_read`` JSON
    extraction without log archaeology.

    Args:
        errors: List of structured error/skip strings from ParsedFile.errors.

    Returns:
        JSON string (e.g. '{"stage": 2, "unknown_sentinel": 5}') or None.

    Examples:
        >>> skip_counts_json(["col_lineage_skip:stage:/a.sql", "col_lineage_skip:stage:/b.sql"])
        '{"stage": 2}'
        >>> skip_counts_json([])
        None
    """
    _PREFIX = "col_lineage_skip:"
    counts: dict[str, int] = {}
    for msg in errors:
        if not msg.startswith(_PREFIX):
            continue
        rest = msg[len(_PREFIX) :]
        # Extract the reason prefix (up to the next ':', or the whole remainder)
        reason = rest.split(":", 1)[0]
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    return json.dumps(counts) if counts else None
