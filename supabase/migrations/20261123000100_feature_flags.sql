-- Feature flags（Assignment 10）：trip_plan（出國換匯計畫）、quick_target（一鍵設定目標價）。
--
-- 網頁、RLS、排程讀的是同一張表，改一列就生效，不用重新部署（使用者重新整理頁面後）。
--   * enabled = false → kill switch：所有人關閉（包括 admin、beta）
--   * allowed_roles   → 這些角色不看 rollout，一律開啟（profiles.role，20261122000000_user_roles）
--   * rollout 0～100  → 其他登入的使用者依 user_id 分桶，同一個人每次結果都一樣
--   * 沒登入          → 一律關閉
-- 分桶算法和 src/fx_alert/flags.py 相同（tests/test_flags.py 用這裡算出的值比對）。
--
-- 只有 SQL Editor（postgres）和 secret key（service_role）能改 flag：
--   update public.feature_flags set enabled = false where flag = 'trip_plan';           -- kill switch
--   update public.feature_flags set enabled = true, rollout = 20 where flag = 'quick_target';

-- ---------------------------------------------------------------------------
-- 1. feature_flags
-- ---------------------------------------------------------------------------
create table public.feature_flags (
  flag          text primary key check (flag ~ '^[a-z][a-z0-9_]{0,39}$'),
  enabled       boolean not null default false,
  rollout       int not null default 0 check (rollout between 0 and 100),
  allowed_roles text[] not null default '{admin,beta}'
                  check (allowed_roles <@ array['user', 'beta', 'admin']),
  updated_at    timestamptz not null default now()
);

comment on table public.feature_flags is
  '功能開關。任何人可讀；只有 SQL Editor（postgres）和 service_role 可改。';

-- 監控用：最後一次切換的時間
create function public.handle_feature_flag_update()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

create trigger on_feature_flag_update
  before update on public.feature_flags
  for each row execute function public.handle_feature_flag_update();

revoke all on function public.handle_feature_flag_update() from public, anon, authenticated;

-- ---------------------------------------------------------------------------
-- 2. 權限：任何人可讀，沒有 insert/update/delete 權限也沒有對應 policy
-- ---------------------------------------------------------------------------
revoke all on public.feature_flags from anon, authenticated;
grant select on public.feature_flags to anon, authenticated;
grant all on public.feature_flags to service_role;

alter table public.feature_flags enable row level security;

create policy "flags are public" on public.feature_flags for select
  to anon, authenticated using (true);

-- ---------------------------------------------------------------------------
-- 3. 初始值：部署到正式專案後，使用者看到的和部署前一樣
--   * trip_plan 已經上線（20261116195500），維持全開；flag 的用途是 kill switch
--   * quick_target 是新功能，先只開給 admin、beta（rollout 0）
-- ---------------------------------------------------------------------------
insert into public.feature_flags (flag, enabled, rollout) values
  ('trip_plan',    true, 100),
  ('quick_target', true,   0);

-- ---------------------------------------------------------------------------
-- 4. is_feature_on(flag)：目前登入的使用者能不能用這個功能
--    網頁用 rpc('is_feature_on')，RLS policy 也呼叫它（伺服器端檢查）。
--    角色用 Task 2 的 public.current_user_role()（20261122000000_user_roles）。
--    security definer：anon 不能呼叫 current_user_role()，這裡沒登入時先回 false、不會呼叫到。
-- ---------------------------------------------------------------------------
create function public.is_feature_on(p_flag text)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select coalesce((
    select f.enabled
       and (select auth.uid()) is not null
       and (
         coalesce((select public.current_user_role()) = any (f.allowed_roles), false)
         or ('x' || substr(md5(f.flag || ':' || (select auth.uid())::text), 1, 8))
              ::bit(32)::bigint % 100 < f.rollout
       )
      from public.feature_flags f
     where f.flag = p_flag
  ), false);
$$;

revoke all on function public.is_feature_on(text) from public;
grant execute on function public.is_feature_on(text) to anon, authenticated, service_role;
