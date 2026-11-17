-- 20261116142000_notify_window 確實已套用（staging 在 SQL Editor 手動套用）
select 'public.profiles.notify_start' as item, exists (
  select 1 from information_schema.columns
  where table_schema = 'public' and table_name = 'profiles' and column_name = 'notify_start') as ok
union all
select 'public.profiles.notify_end', exists (
  select 1 from information_schema.columns
  where table_schema = 'public' and table_name = 'profiles' and column_name = 'notify_end')
union all
select 'constraint profiles_notify_window_half_hour', exists (
  select 1 from pg_constraint
  where conname = 'profiles_notify_window_half_hour' and conrelid = 'public.profiles'::regclass)
union all
select 'active_rules 有 notify_start', exists (
  select 1 from information_schema.columns
  where table_schema = 'public' and table_name = 'active_rules' and column_name = 'notify_start')
