-- 20261109000000_multi_user_schema 確實已套用：四張資料表存在、而且都開了 RLS
select 'table ' || t as item,
       coalesce((select c.relrowsecurity from pg_class c
                 where c.oid = to_regclass('public.' || t)), false) as ok
from unnest(array['currencies', 'profiles', 'alert_rules', 'notifications']) as t
