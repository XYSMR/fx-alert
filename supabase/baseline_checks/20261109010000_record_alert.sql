-- 20261109010000_record_alert 確實已套用
select 'function public.record_alert(bigint, numeric, numeric, timestamptz)' as item,
       to_regprocedure('public.record_alert(bigint, numeric, numeric, timestamptz)') is not null as ok
