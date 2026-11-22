-- quick_target 的正式站預設：關閉（enabled = false, rollout = 0）。PM 2026-11-23 決定。
--
-- 20261123000100 的初始值是 enabled = true, rollout = 0（只開給 admin、beta）。
-- 那個檔案已經套用到 staging，內容不能再改（部署 workflow 會比對），所以用這個 migration 修正。
-- 版本號排在 Task 2（PR #50）的 20261123000200、000300 之前：部署 workflow 要求待套用的版本
-- 比已套用的都新，所以這個 PR 必須比 #50 先合併到 main，正式專案才會依序套用。
-- 部署到正式專案後：trip_plan = true / 100（已上線，flag 只當 kill switch），quick_target = false / 0。
--
-- staging 要讓 admin、beta 測試 quick_target 時，在 staging 的 SQL Editor 執行
-- supabase/staging/feature_flags.sql（不是 migration，不會部署到正式專案）。

update public.feature_flags
   set enabled = false, rollout = 0
 where flag = 'quick_target';
