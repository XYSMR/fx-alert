-- staging 專用的 flag 設定（A10 測試用）。只在 staging 的 SQL Editor 執行。
--
-- ⚠️ 執行前確認左上角的專案是 staging。這個檔案不是 migration，部署 workflow 不會執行它，
--    正式專案的值只由 supabase/migrations 決定：trip_plan = true / 100、quick_target = false / 0。
--
-- 和正式專案的差別只有一個：quick_target 開給 admin、beta（rollout 仍然是 0，一般使用者看不到）。
-- 要把某個人設成 beta：
--   update public.profiles set role = 'beta' where email = '...';

update public.feature_flags
   set enabled = true, rollout = 100, allowed_roles = '{admin,beta}'
 where flag = 'trip_plan';

update public.feature_flags
   set enabled = true, rollout = 0, allowed_roles = '{admin,beta}'
 where flag = 'quick_target';

-- 驗證（截圖）：staging 設定、和正式專案預設的差異
select f.flag,
       f.enabled,
       f.rollout,
       f.allowed_roles,
       case f.flag
         when 'trip_plan'    then 'true / 100'
         when 'quick_target' then 'false / 0'
       end as production_default,
       f.updated_at
  from public.feature_flags f
 order by f.flag;
