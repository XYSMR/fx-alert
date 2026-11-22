-- trip_plan 關閉時，出國換匯計畫變成「唯讀」（和前端的唯讀 UI 一致）：
-- 旅程（trips）和換匯紀錄（trip_exchanges）都不能新增、修改、刪除，只能看。
--
-- 全部用 RESTRICTIVE（和原本 permissive 的「自己的資料」是「而且」）。
-- 回應的差別（PostgREST）：
--   * 新增、修改：WITH CHECK 不通過 → 錯誤 42501 → HTTP 403
--   * 刪除：只有 USING，RLS 會把列「藏起來」→ 不會報錯，回 200/204 但刪掉 0 列（資料還在）
--   修改的 USING 寫 true，讓檢查落在 WITH CHECK，才會明確回 403 而不是默默改 0 列。
--
-- 不受影響：
--   * 讀取（select）：flag 關閉時照樣看得到自己的資料
--   * 排程（service_role）和刪除帳號的連帶刪除：service_role 與外鍵 cascade 不經過 RLS
--
-- trips 的新增已經由 20261123000200 擋住，這裡補上其餘五種寫入。

create policy "trips update only when trip_plan is on"
  on public.trips as restrictive for update to authenticated
  using (true)
  with check ((select public.is_feature_on('trip_plan')));

create policy "trips delete only when trip_plan is on"
  on public.trips as restrictive for delete to authenticated
  using ((select public.is_feature_on('trip_plan')));

create policy "exchanges insert only when trip_plan is on"
  on public.trip_exchanges as restrictive for insert to authenticated
  with check ((select public.is_feature_on('trip_plan')));

create policy "exchanges update only when trip_plan is on"
  on public.trip_exchanges as restrictive for update to authenticated
  using (true)
  with check ((select public.is_feature_on('trip_plan')));

create policy "exchanges delete only when trip_plan is on"
  on public.trip_exchanges as restrictive for delete to authenticated
  using ((select public.is_feature_on('trip_plan')));
