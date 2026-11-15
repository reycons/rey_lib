"""Opt-in PostgreSQL verification of the actual bounded graph SQL using TEMP fixtures only."""
import os
import re

import pytest


def _body(cursor, function):
    """The statement, now the database function's own body (#1360)."""
    cursor.execute("SELECT pg_get_functiondef(to_regprocedure(%s))", (function,))
    return cursor.fetchone()[0].split("$function$")[1]


pytestmark = pytest.mark.skipif(not os.environ.get("REY_TEST_POSTGRES_DSN"),
                                reason="Requires an explicit PostgreSQL TEMP-fixture test connection")


def test_the_actual_sql_bounds_views_code_and_tables_and_scopes_the_bridge():
    psycopg = pytest.importorskip("psycopg")
    tables={
    'db_object_vw':'db_node_id bigint, db_source_id bigint, catalog_name text, provider text, schema_name text, object_name text, object_type text, signature text',
    'db_source':'db_source_id bigint, catalog_name text, provider text',
    'db_code_reference':'code_db_code_reference_id bigint, code_symbol_id bigint, code_file_id bigint, repository_key text, relative_path text, qualified_name text, seam text, binding_name text',
    'file':'code_file_id bigint, repository_id bigint, relative_path text',
    'repository':'repository_id bigint, repository_key text',
    'symbol':'code_symbol_id bigint, code_file_id bigint, qualified_name text',
    'db_reference_vw':'code_db_reference_id bigint, source_node_id bigint, target_node_id bigint, reference_kind text',
    'db_bridge_coverage_vw':'installation text, map_name text, seam text',
    'db_binding_vw':'code_db_binding_id bigint, binding_name text, map_name text, installation text, db_node_id bigint, resolves_now boolean',
    'relationship_vw':'code_relationship_id bigint, source_code_symbol_id bigint, target_code_symbol_id bigint, target_code_file_id bigint, is_resolved boolean, is_test boolean, edge_kind text',
    }
    c=psycopg.connect(os.environ['REY_TEST_POSTGRES_DSN'])
    try:
     with c.cursor() as q:
      sql=_body(q,'code.f_object_map_table_context(character varying, character varying, '
                  'character varying, character varying, text, character varying, character varying, character varying)')
      q.execute("SET LOCAL statement_timeout = '20s'")
      for table,cols in tables.items():
       q.execute(f'CREATE TEMP TABLE ctx_{table} ({cols}) ON COMMIT DROP')
       sql=re.sub(r'\bcode\.'+table+r'\b','pg_temp.ctx_'+table,sql)
      q.execute("INSERT INTO pg_temp.ctx_db_source VALUES (1,'fixture','postgres'),(2,'other','postgres')")
      objects=[(1,'root','table'),(2,'linked','table'),(3,'third_table','table'),(4,'view1','view'),(5,'view2','view'),(6,'view3','view'),(7,'routine','function'),(8,'incoming','table')]
      q.executemany('INSERT INTO pg_temp.ctx_db_object_vw VALUES (%s,1,\'fixture\',\'postgres\',\'public\',%s,%s,\'\')',objects)
      q.execute("INSERT INTO pg_temp.ctx_db_object_vw VALUES (70,1,'fixture','postgres','public','routine','function','integer')")
      q.execute("INSERT INTO pg_temp.ctx_db_object_vw VALUES (999,2,'other','postgres','public','root','table','')")
      refs=[(1,1,2,'foreign_key'),(2,2,3,'foreign_key'),(3,4,1,'read'),(4,5,4,'read'),(5,6,5,'read'),(6,7,1,'read'),(7,8,1,'foreign_key'),(8,7,4,'read')]
      q.executemany('INSERT INTO pg_temp.ctx_db_reference_vw VALUES (%s,%s,%s,%s)',refs)
      q.execute("INSERT INTO pg_temp.ctx_repository VALUES (1,'app')")
      q.execute("INSERT INTO pg_temp.ctx_file VALUES (100,1,'main.py')")
      q.executemany('INSERT INTO pg_temp.ctx_symbol VALUES (%s,100,%s)',[(10,'routine_user'),(20,'direct'),(21,'caller'),(22,'third')])
      q.execute("INSERT INTO pg_temp.ctx_db_bridge_coverage_vw VALUES ('admin','control','control_dispatch')")
      q.execute("INSERT INTO pg_temp.ctx_db_binding_vw VALUES (1,'use_routine','control','admin',7,true),(2,'direct','control','admin',1,true),(3,'direct','wrong_map','admin',999,true),(4,'direct','control','other_installation',999,true)")
      q.execute("INSERT INTO pg_temp.ctx_db_code_reference VALUES (1,10,100,'app','main.py','routine_user','control_dispatch','use_routine'),(2,20,100,'app','main.py','direct','control_dispatch','direct')")
      q.execute("INSERT INTO pg_temp.ctx_relationship_vw VALUES (1,21,20,NULL,true,false,'call'),(2,22,21,NULL,true,false,'call')")
      sql=re.sub(r'\bp_(catalog|provider|schema|table|node|installation|object_type|signature)\b',r'%(\1)s',sql)
      params={'catalog':'fixture','provider':'postgres','schema':'public','table':'root','installation':'admin','node':None,
              'object_type':'table','signature':''}
      q.execute(sql,params)
      count,root,nodes,edges=q.fetchone()
      ids={n['id'] for n in nodes}
      assert count==1 and root=='db:1'
      assert ids=={'db:1','db:2','db:4','db:5','db:7','db:8','symbol:10','symbol:20','symbol:21'},ids
      assert len(ids)==len(nodes),'duplicate node identities'
      assert all(e['source'] in ids and e['target'] in ids for e in edges)
      assert any(e['source']=='db:5' and e['target']=='db:4' for e in edges)
      assert any(e['source']=='symbol:21' and e['target']=='symbol:20' for e in edges)
      # The same operation reroots on a view, SQL routine, or application symbol.
      params.update(node='db:4',table=None)
      q.execute(sql,params)
      count,root,nodes,edges=q.fetchone()
      assert count==1 and root=='db:4'
      ids={n['id'] for n in nodes}
      assert {'db:1','db:4','db:5','db:6','db:7','symbol:10'} <= ids
      assert 'db:2' not in ids and 'db:8' not in ids
      params['node']='symbol:20'
      q.execute(sql,params)
      count,root,nodes,edges=q.fetchone()
      assert count==1 and root=='symbol:20'
      assert {n['id'] for n in nodes}=={'symbol:20','symbol:21','symbol:22','db:1'}
      params['node']='db:7'
      q.execute(sql,params)
      count,root,nodes,edges=q.fetchone()
      assert count==1 and root=='db:7'
      assert {'db:1','db:4','symbol:10'} <= {n['id'] for n in nodes}
      params['node']='symbol:999999'
      q.execute(sql,params)
      assert q.fetchone()==(0,None,[],[])
      params.update(node=None,table='root')
      params['installation']='no_access'
      q.execute(sql,params)
      _,_,nodes,_=q.fetchone()
      assert not any(n['id'].startswith('symbol:') for n in nodes)
      params['catalog']='missing'
      q.execute(sql,params)
      assert q.fetchone()==(0,None,[],[])
      params.update(catalog='fixture',table='view1',object_type='view',signature='')
      q.execute(sql,params)
      assert q.fetchone()[1]=='db:4'
      params.update(table='routine',object_type='function',signature='integer')
      q.execute(sql,params)
      assert q.fetchone()[1]=='db:70'
      params['signature']=''
      q.execute(sql,params)
      assert q.fetchone()[1]=='db:7'
      params['signature']='text'
      q.execute(sql,params)
      assert q.fetchone()==(0,None,[],[])
      print('PASS: inbound/outbound tables one hop; dependent views and SQL/application code two hops; third hops excluded; bridge installation/map scope; missing root; unique ids and complete edges.')
    finally:
     c.rollback()
     c.close()
     print('Temporary fixtures rolled back; no estate records changed.')
