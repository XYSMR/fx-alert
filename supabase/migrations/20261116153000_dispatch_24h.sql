-- 24 小時檢查：每天（含週末）每 30 分鐘觸發一次 check-rates（台灣時間 :05、:35）。
--
-- 原本（20261110020000）：
--   fx-alert-dispatch       '5,35 1-7 * * 1-5'  台灣平日 09:05–15:35
--   fx-alert-dispatch-1605  '5 8 * * 1-5'       台灣平日 16:05
-- 改成一個工作：
--   fx-alert-dispatch       '5,35 * * * *'      每天 00:05、00:35 … 23:05、23:35（UTC 和台灣的分鐘數一樣）
--
-- 夜裡到價會不會半夜寄信：每個人的通知時段（20261116142000，預設 08:00–22:00）決定；
-- 時段外到價不寄，時段開始後重新判斷。
--
-- 週末：牌告沒有變動時不會 commit、不會部署（publish.py），只多一次 Actions 執行。
-- README 待辦：11/30 之後統計週末的牌告變動次數，再評估要不要改回只跑平日。
--
-- cron.schedule 用同一個名稱會更新原本的排程；重複執行也不會多出工作。

select cron.schedule('fx-alert-dispatch', '5,35 * * * *', 'select private.dispatch_check_rates()');

-- 16:05 那個工作已經包含在上面，拿掉。不存在（例如重複執行）就略過。
-- 用 cron.unschedule(名稱)：不需要讀 cron.job 的權限（Supabase 上 postgres 讀不到 cron.job）
do $$
begin
  perform cron.unschedule('fx-alert-dispatch-1605');
exception when others then
  raise notice 'fx_alert: fx-alert-dispatch-1605 不存在，略過';
end;
$$;
