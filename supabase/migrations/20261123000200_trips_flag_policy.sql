-- 伺服器端檢查：trip_plan 關閉時，不能新增旅程（A10 Task 2，TC3）。
--
-- 一定要用 RESTRICTIVE：trips 已經有 permissive 的「insert own trips」，
-- PostgreSQL 的多條 permissive policy 是「或」，再加一條 permissive 擋不住
-- （tests/db/test_feature_flags.py::test_permissive_policy_does_not_block）。
-- restrictive 和 permissive 是「而且」：必須是自己的旅程，而且 trip_plan 對這個人是開的。
--
-- 只擋新增；flag 關閉時已經建立的旅程照樣看得到、改得了、刪得掉（關掉功能不刪使用者的資料）。
-- (select …) 包起來：每個查詢只算一次，不是每一列算一次。
-- 網頁隱藏按鈕只是介面；直接打 API 也會在這裡被擋（PostgREST 回 403）。

create policy "trips only when trip_plan is on"
  on public.trips
  as restrictive
  for insert
  to authenticated
  with check ((select public.is_feature_on('trip_plan')));
