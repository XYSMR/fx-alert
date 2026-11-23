-- A10 Task 3b 的正式測試證據：在 staging 的 SQL Editor 整段執行，結果表格就是證據（截圖）。
--
-- ⚠️ 只在 staging 執行。測試時會暫時改 feature_flags，最後一定還原成執行前的值
--    （中途出錯的話整段 rollback，flag 不會被改到）。測試期間（不到 1 秒）staging 的網頁可能看到切換。
--
-- 用固定的測試 user_id 呼叫 public.is_feature_on()，不需要真的有這些帳號：
--   11111111-2222-3333-4444-555555555555  在 trip_plan 的分桶 = 49（和 tests/test_flags.py 相同）
--
-- 測試項目：
--   C1–C4  兩個 flag 的四種 ON/OFF 組合，彼此獨立
--   K1–K2  kill switch：rollout 100 也會全部關閉，改回來立即恢復
--   B1–B3  固定分桶：同一人 10 次結果相同、分桶 = 49 和 Python 一致、1000 人約 20%
--   S1     沒登入一律關閉

drop table if exists pg_temp.a10_results;
create temp table a10_results (
  seq      serial,
  id       text primary key,
  test     text not null,
  expected text not null,
  actual   text not null
);

-- 用指定的 user_id（null = 沒登入）呼叫 is_feature_on
create or replace function pg_temp.a10_on(p_uid uuid, p_flag text)
returns boolean
language plpgsql
as $$
begin
  perform set_config(
    'request.jwt.claims',
    case when p_uid is null then '{"role": "anon"}'  -- 沒登入：只帶 publishable key 的請求
         else json_build_object('sub', p_uid, 'role', 'authenticated')::text end,
    true);
  return public.is_feature_on(p_flag);
end;
$$;

create or replace function pg_temp.a10_onoff(p boolean)
returns text
language sql
as $$ select case when p then 'ON' else 'OFF' end $$;

do $$
declare
  u   constant uuid := '11111111-2222-3333-4444-555555555555';
  saved record;
  t boolean; q boolean;
  n int;
  combo record;
begin
  -- 記下執行前的值，最後還原
  create temp table a10_saved on commit drop as
    select flag, enabled, rollout, allowed_roles from public.feature_flags;

  -- C1–C4：四種組合（rollout 100，只看 enabled）
  for combo in
    select * from (values
      ('C1', true,  true),
      ('C2', true,  false),
      ('C3', false, true),
      ('C4', false, false)
    ) v(id, trip_on, quick_on)
  loop
    update public.feature_flags set enabled = combo.trip_on,  rollout = 100 where flag = 'trip_plan';
    update public.feature_flags set enabled = combo.quick_on, rollout = 100 where flag = 'quick_target';
    t := pg_temp.a10_on(u, 'trip_plan');
    q := pg_temp.a10_on(u, 'quick_target');
    insert into a10_results (id, test, expected, actual) values (
      combo.id,
      format('trip_plan %s / quick_target %s',
             case when combo.trip_on then 'ON' else 'OFF' end,
             case when combo.quick_on then 'ON' else 'OFF' end),
      format('trip=%s, quick=%s', pg_temp.a10_onoff(combo.trip_on), pg_temp.a10_onoff(combo.quick_on)),
      format('trip=%s, quick=%s', pg_temp.a10_onoff(t), pg_temp.a10_onoff(q)));
  end loop;

  -- K1–K2：kill switch
  update public.feature_flags set enabled = false, rollout = 100 where flag = 'trip_plan';
  insert into a10_results (id, test, expected, actual) values ('K1', 'kill switch: enabled=false, rollout=100',
    'OFF', pg_temp.a10_onoff(pg_temp.a10_on(u, 'trip_plan')));
  update public.feature_flags set enabled = true, rollout = 100 where flag = 'trip_plan';
  insert into a10_results (id, test, expected, actual) values ('K2', 'switch back: enabled=true (no redeploy)',
    'ON', pg_temp.a10_onoff(pg_temp.a10_on(u, 'trip_plan')));

  -- B1–B3：固定分桶
  update public.feature_flags set enabled = true, rollout = 50 where flag = 'trip_plan';
  select count(*) filter (where pg_temp.a10_on(u, 'trip_plan')) into n from generate_series(1, 10);
  insert into a10_results (id, test, expected, actual) values ('B1', 'sticky: same user 10 times, rollout=50 (bucket 49)',
    '10 / 10 on', n || ' / 10 on');

  update public.feature_flags set rollout = 49 where flag = 'trip_plan';
  insert into a10_results (id, test, expected, actual) values ('B2', 'bucket = 49 (same as Python): rollout=49 → off',
    'OFF', pg_temp.a10_onoff(pg_temp.a10_on(u, 'trip_plan')));

  update public.feature_flags set rollout = 20 where flag = 'trip_plan';
  select count(*) filter (where pg_temp.a10_on(gen_random_uuid(), 'trip_plan')) into n
    from generate_series(1, 1000);
  insert into a10_results (id, test, expected, actual) values ('B3', 'rollout=20: 1000 random users',
    '140–260 on (≈20%)', n || ' on');  -- 1000 人的 20%，±60 約是 ±4.7σ

  -- S1：沒登入
  update public.feature_flags set enabled = true, rollout = 100 where flag = 'trip_plan';
  insert into a10_results (id, test, expected, actual) values ('S1', 'signed out (no JWT), rollout=100',
    'OFF', pg_temp.a10_onoff(pg_temp.a10_on(null, 'trip_plan')));

  -- 還原
  for saved in select * from a10_saved loop
    update public.feature_flags
       set enabled = saved.enabled, rollout = saved.rollout, allowed_roles = saved.allowed_roles
     where flag = saved.flag;
  end loop;
  perform set_config('request.jwt.claims', '{"role": "anon"}', true);
end;
$$;

-- 結果（截圖）
select id,
       test,
       expected,
       actual,
       case
         when id = 'B3' then
           case when split_part(actual, ' ', 1)::int between 140 and 260 then 'PASS' else 'FAIL' end
         when expected = actual then 'PASS'
         else 'FAIL'
       end as result
  from a10_results
 order by seq;
