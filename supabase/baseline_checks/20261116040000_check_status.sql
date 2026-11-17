-- 20261116040000_check_status 確實已套用（staging 在 SQL Editor 手動套用）
select 'table public.check_status' as item, to_regclass('public.check_status') is not null as ok
union all
select 'check_status 開啟 RLS', coalesce(
  (select relrowsecurity from pg_class where oid = to_regclass('public.check_status')), false)
union all
select 'policy check status is public', exists (
  select 1 from pg_policies
  where schemaname = 'public' and tablename = 'check_status' and policyname = 'check status is public')
