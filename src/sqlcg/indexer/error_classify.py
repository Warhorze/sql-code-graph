"""Error classification for lineage extraction.

Maps structured error messages recorded during parsing into E-code buckets
for measurement and summary reporting.
"""


def _classify_error(msg: str) -> str:
    """Map a structured error message to its E-code or skip-reason bucket.

    Buckets (defined in ARCHITECTURE_REVIEW.md § 12.3 + sprint 09 plan):
      - "E1": col_lineage:NULL:Cannot find column 'NULL' (driven to 0 by sprint 08 T-05)
      - "E2": col_lineage:...:Cannot find column '...' with '(' or non-identifier chars
      - "E3": col_lineage:...:Expecting / Invalid expression / Unexpected token
      - "E5": col_lineage:...:Cannot find column (plain identifier)
      - "E8": col_lineage_skip:dynamic_source
      - "timeout": timeout:Ns
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

    # Timeout errors
    if msg.startswith("timeout:"):
        return "timeout"

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
