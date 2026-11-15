-- 20261109020000_active_rules 確實已套用
select 'view public.active_rules' as item,
       exists (select 1 from pg_views where schemaname = 'public' and viewname = 'active_rules') as ok
