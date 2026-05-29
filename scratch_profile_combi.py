"""Profile parsing of COMBI_WEEK_BOUWMARKT.sql — do NOT commit."""
from pathlib import Path
from sqlcg.parsers.snowflake_parser import SnowflakeParser
from sqlcg.lineage.schema_resolver import SchemaResolver

sql = Path("/home/ignwrad/Projects/dwh/ddl/changelogs/IA-DATAPRODUCTS/COMBI_WEEK_BOUWMARKT.sql").read_text()
resolver = SchemaResolver(dialect="snowflake")
parser = SnowflakeParser(resolver)

result = parser.parse_file(Path("COMBI_WEEK_BOUWMARKT.sql"), sql)
print(f"statements : {len(result.statements)}")
print(f"errors     : {len(result.errors)}")
print(f"quality    : {result.parse_quality}")
if result.statements:
    s = result.statements[0]
    print(f"col_lineage: {len(s.column_lineage)}")
    print(f"sources    : {len(s.sources)}")
