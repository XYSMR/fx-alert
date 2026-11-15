-- 20261111000000_dispatch_source 確實已套用：pg_cron 送出的 inputs 帶 source=pg_cron、force=false
select 'private.dispatch_inputs(''check-rates.yml'') 帶 source=pg_cron' as item,
       coalesce(private.dispatch_inputs('check-rates.yml') ->> 'source' = 'pg_cron'
                and private.dispatch_inputs('check-rates.yml') ->> 'force' = 'false', false) as ok
