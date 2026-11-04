"""Request inventory uses indexed host construction and preserves guard direction."""
import os

import pytest
from rey_lib.inferred_architecture.object_map import _SQL_DIR

pytestmark = pytest.mark.skipif(not os.environ.get('REY_TEST_POSTGRES_DSN'),
                                reason='Requires an explicit PostgreSQL TEMP-fixture connection')


def test_request_inventory_inherits_only_the_proved_host_and_never_becomes_cli():
    psycopg = pytest.importorskip('psycopg')
    connection = psycopg.connect(os.environ['REY_TEST_POSTGRES_DSN'])
    try:
        with connection.cursor() as cursor:
            cursor.execute('CREATE TEMP TABLE control_flow_node_vw ('
                           'repository_key text,relative_path text,code_symbol_id bigint,node_key text,'
                           'dotted_identity text,kind text,commands text[],coverage_status text,'
                           'coverage_reason text,source_line integer) ON COMMIT DROP')
            cursor.execute('CREATE TEMP TABLE relationship_vw ('
                           'repository_key text,relative_path text,is_test boolean,edge_kind text,'
                           'target_repository_key text,target_identity text) ON COMMIT DROP')
            cursor.execute('CREATE TEMP TABLE object_map_flow_command_vw ('
                           'application_name text,application_command_id bigint,command_name text,'
                           'description text,ordinal integer) ON COMMIT DROP')
            host = 'rey_lib.run.hosted_objects.HostedObjects'
            cursor.executemany('INSERT INTO pg_temp.control_flow_node_vw VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', [
                ('ai_app','serve.py',10,'run','ai_app.serve.Task.answer','condition',['request:run'],'complete',None,10),
                ('ai_app','serve.py',11,'task','ai_app.serve.AI.answer','condition',['request-false:task'],'complete',None,20),
                ('ai_app','main.py',12,'entry','main.main','entry',['serve'],'complete',None,1),
                ('rey_lib','hosted.py',20,'projection',host+'._answer','condition',['request:projection'],'complete',None,30),
                ('rey_lib','hosted.py',21,'close',host+'.serve','condition',['request:close'],'complete',None,40),
                ('rey_lib','other.py',22,'bogus','rey_lib.Other.answer','condition',['request:unrelated'],'complete',None,50),
            ])
            cursor.executemany('INSERT INTO pg_temp.relationship_vw VALUES (%s,%s,%s,%s,%s,%s)', [
                ('ai_app','serve.py',False,'call','rey_lib',host),
                ('ai_app','serve.py',False,'call','rey_lib',host),
                ('ai_app','main.py',False,'call',None,None),
                ('plain_app','main.py',False,'import','rey_lib',host),
                ('test_app','test_host.py',True,'call','rey_lib',host),
                ('rey_lib','hosted.py',False,'call',None,None),
                ('rey_lib','other.py',False,'call',None,None),
            ])
            cursor.execute("INSERT INTO pg_temp.object_map_flow_command_vw VALUES ('ai_app',1,'serve',NULL,1)")
            def read(name):
                sql = (_SQL_DIR / name).read_text().replace('code.', 'pg_temp.')
                cursor.execute(sql)
                names = [column.name for column in cursor.description]
                return [dict(zip(names, row)) for row in cursor.fetchall()]
            rows = read('object_map_flow_requests.sql')
            inputs = [r for r in rows if r['application_name'] == 'ai_app']
            assert sorted(r['request_name'] for r in inputs) == ['close','projection','run','task']
            assert next(r for r in inputs if r['request_name']=='task')['request_branch'] == 'false'
            assert next(r for r in inputs if r['request_name']=='task')['application_request_id'] == '11:task'
            assert next(r for r in inputs if r['request_name']=='projection')['repository_key'] == 'rey_lib'
            assert not any(r['application_name'] in ('plain_app','test_app') for r in rows)
            commands = read('object_map_flow_commands.sql')
            assert [(r['application_name'],r['command_name']) for r in commands] == [('ai_app','serve')]
    finally:
        connection.rollback()
        connection.close()
