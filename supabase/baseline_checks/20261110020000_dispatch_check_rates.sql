-- 20261110020000_dispatch_check_rates 確實已套用（pg_cron 外部觸發；2026-11-10 在 SQL Editor 手動套用）
-- 用物件本身判斷（不查 pg_extension），本機測試用的 pg_cron／pg_net 替身也適用。
-- cron.job 不存在時這段會直接出錯，部署一樣會停止。
select 'pg_cron：cron.job 資料表' as item, to_regclass('cron.job') is not null as ok
union all
select 'pg_net：net.http_post', exists (
  select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
  where n.nspname = 'net' and p.proname = 'http_post')
union all
select 'function private.dispatch_inputs(text)',
       to_regprocedure('private.dispatch_inputs(text)') is not null
union all
select 'function private.dispatch_check_rates()',
       to_regprocedure('private.dispatch_check_rates()') is not null
union all
select 'cron job fx-alert-dispatch（5,35 1-7 * * 1-5）',
       exists (select 1 from cron.job where jobname = 'fx-alert-dispatch')
union all
select 'cron job fx-alert-dispatch-1605（5 8 * * 1-5）',
       exists (select 1 from cron.job where jobname = 'fx-alert-dispatch-1605')
