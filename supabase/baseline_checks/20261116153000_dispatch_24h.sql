-- 20261116153000_dispatch_24h 確實已套用（staging 在 SQL Editor 手動套用）
-- cron.job 不存在時這段會直接出錯，部署一樣會停止。
select 'cron job fx-alert-dispatch 每天 24 小時（5,35 * * * *）' as item,
       exists (select 1 from cron.job
               where jobname = 'fx-alert-dispatch' and schedule = '5,35 * * * *') as ok
union all
select '舊的 fx-alert-dispatch-1605 已刪除',
       not exists (select 1 from cron.job where jobname = 'fx-alert-dispatch-1605')
