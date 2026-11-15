-- 20261110010000_import_rules_for_user 確實已套用
select 'function public.import_rules_for_user(uuid, jsonb, jsonb, boolean)' as item,
       to_regprocedure('public.import_rules_for_user(uuid, jsonb, jsonb, boolean)') is not null as ok
