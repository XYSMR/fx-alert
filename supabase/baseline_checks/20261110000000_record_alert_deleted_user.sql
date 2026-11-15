-- 20261110000000_record_alert_deleted_user 確實已套用：record_alert 新版會吃下 foreign_key_violation
--（帳號在排程期間被刪除時回傳 null，不讓整次排程失敗）
select 'function public.record_alert(...) 處理 foreign_key_violation' as item,
       coalesce(pg_get_functiondef(to_regprocedure(
         'public.record_alert(bigint, numeric, numeric, timestamptz)')) ilike '%foreign_key_violation%',
         false) as ok
