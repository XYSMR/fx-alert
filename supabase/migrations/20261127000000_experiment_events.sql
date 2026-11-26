-- HDD 指標：記錄 A10 兩個假設需要的使用者行為（A11，issue #53）。
--
--   H1 trip_plan     ：有沒有建立旅程                      → trip_created
--   H2 quick_target  ：有沒有按一鍵目標價、多快存下第一個提醒 → quick_target_clicked、alert_saved
--   兩者的分母        ：有多少人看到（登入後載入頁面記一次）   → exposed（ON、OFF 兩組都記）
--
-- 保留原始資料、不擋重複：同一個人可以有很多筆。
--   ⚠ 算指標一律用「不重複的使用者數」（count(distinct user_id)）和「每人的第一筆」，
--     不要直接 count(*)。
-- 前端只用 .insert(...)，不要接 .select()：使用者沒有讀取權限，接了會 permission denied。
--
-- 權限（RLS）：
--   * 使用者只能「新增自己的事件」，而且只能填 event、flag 兩欄；
--     不能讀、不能改、不能刪（指標只有 service_role 在 SQL Editor / 報表裡算）。
--   * user_id 預設 auth.uid()，使用者沒有這欄的寫入權限，policy 再檢查一次是自己。
--   * variant（on / off）由 trigger 依伺服器端的 is_feature_on() 決定，
--     不相信瀏覽器送來的值，使用者沒辦法假裝自己在實驗組。
-- 刪除帳號時，事件跟著 auth.users 連帶刪除（on delete cascade）。

create table public.experiment_events (
  id         bigint generated always as identity primary key,
  user_id    uuid not null default auth.uid() references auth.users (id) on delete cascade,
  event      text not null
             constraint experiment_events_event_valid
             check (event in ('exposed', 'trip_created', 'quick_target_clicked', 'alert_saved')),
  flag       text not null
             constraint experiment_events_flag_valid
             check (flag in ('trip_plan', 'quick_target')),
  variant    text not null default 'off'
             constraint experiment_events_variant_valid
             check (variant in ('on', 'off')),
  created_at timestamptz not null default now(),
  -- 事件要和實驗配對：H1 的事件不能被算進 H2（review by @Hank0903）
  constraint experiment_events_event_flag_pair check (
    event = 'exposed'
    or (event = 'trip_created' and flag = 'trip_plan')
    or (event in ('quick_target_clicked', 'alert_saved') and flag = 'quick_target')
  )
);

comment on table public.experiment_events is
  'HDD 指標事件：使用者只能新增自己的事件；variant 由伺服器決定；只有 service_role 能讀';

create index experiment_events_flag_event_idx
  on public.experiment_events (flag, event, created_at);

-- variant 由伺服器決定；created_at 一律用資料庫時間
create function public.set_experiment_variant()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.variant := case when public.is_feature_on(new.flag) then 'on' else 'off' end;
  new.created_at := now();
  return new;
end;
$$;

revoke all on function public.set_experiment_variant() from public, anon, authenticated;

create trigger experiment_events_set_variant
  before insert on public.experiment_events
  for each row execute function public.set_experiment_variant();

alter table public.experiment_events enable row level security;

revoke all on public.experiment_events from anon, authenticated;
grant insert (event, flag) on public.experiment_events to authenticated;
grant all on public.experiment_events to service_role;

create policy "insert own events"
  on public.experiment_events
  for insert
  to authenticated
  with check (user_id = (select auth.uid()));
