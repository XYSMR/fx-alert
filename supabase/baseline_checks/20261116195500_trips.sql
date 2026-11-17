-- 20261116195500_trips 確實已套用（staging 在 SQL Editor 手動套用）
select 'table public.trips' as item, to_regclass('public.trips') is not null as ok
union all
select 'table public.trip_exchanges', to_regclass('public.trip_exchanges') is not null
union all
select 'table public.trip_notifications', to_regclass('public.trip_notifications') is not null
union all
select 'view public.trip_progress', to_regclass('public.trip_progress') is not null
union all
select 'view public.active_trips', to_regclass('public.active_trips') is not null
union all
select '三張表都開啟 RLS', (
  select count(*) = 3 from pg_class
  where oid in (to_regclass('public.trips'), to_regclass('public.trip_exchanges'),
                to_regclass('public.trip_notifications'))
    and relrowsecurity)
union all
select 'trigger on_trip_insert', exists (select 1 from pg_trigger where tgname = 'on_trip_insert')
union all
select 'trigger on_trip_exchange_insert',
       exists (select 1 from pg_trigger where tgname = 'on_trip_exchange_insert')
union all
select '固定提醒每趟一次（trip_notifications_fixed_once）',
       to_regclass('public.trip_notifications_fixed_once') is not null
