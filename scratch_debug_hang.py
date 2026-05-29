"""Verify the deadlock fix for subprocess pipe overflow. Do NOT commit."""
import sys, time, multiprocessing as mp, queue
sys.path.insert(0, '/home/ignwrad/Projects/sql-code-graph/src')
from pathlib import Path
from sqlcg.indexer.indexer import Indexer
from sqlcg.parsers.snowflake_parser import SnowflakeParser
from sqlcg.lineage.schema_resolver import SchemaResolver


def main():
    slow_files = [
        '/home/ignwrad/Projects/dwh/ddl/changelogs/IA-SEMANTIC/ARTIKEL.sql',
        '/home/ignwrad/Projects/dwh/ddl/changelogs/IA-SEMANTIC/ODS_WORKAROUND_ARTIKELDIMENSIE.sql',
        '/home/ignwrad/Projects/dwh/ddl/changelogs/BA-TABLES/WTDH_ARTIKEL.sql',
        '/home/ignwrad/Projects/dwh/ddl/changelogs/IA-DATAPRODUCTS/COMBI_WEEK_BOUWMARKT.sql',
        '/home/ignwrad/Projects/dwh/etl/sql/fact/wtfe_verkoopinfo_2.sql',
    ]

    resolver = SchemaResolver(dialect='snowflake')
    parser = SnowflakeParser(resolver)
    indexer = Indexer()

    for f in slow_files:
        p = Path(f)
        sql = p.read_text()
        t0 = time.perf_counter()
        result = indexer._index_single_file(parser, p, sql, timeout=30)
        dt = time.perf_counter() - t0
        edges = sum(len(s.column_lineage) for s in result.statements)
        flag = '  <<STILL_SLOW' if dt > 5 else ''
        print(f'{dt:6.2f}s  {len(result.statements):3d} stmts  {edges:5d} edges  {p.name}{flag}', flush=True)


if __name__ == '__main__':
    main()
