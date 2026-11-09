-- fx-alert 多人版：資料表、權限、RLS
--
-- 原則
--   * 個人資料（email、目標價、狀態、通知紀錄）只存在這裡，永遠不進 git。
--   * 所有表都開 RLS；先把 Supabase 預設給 anon / authenticated 的權限全部收回，
--     再只開放需要的「表 + 欄位」。
--   * 排程用 secret key（service_role，會略過 RLS）讀寫狀態；
--     使用者用 publishable key + 登入 JWT（authenticated）只能碰自己的資料。
--   * 刪除 auth.users 的一列 → profiles → alert_rules / notifications 全部連帶刪除。

-- ---------------------------------------------------------------------------
-- 1. currencies：可以追蹤的幣別（公開資料）
-- ---------------------------------------------------------------------------
create table public.currencies (
  code    text primary key check (code ~ '^[A-Z]{3}$'),
  name_zh text not null
);

insert into public.currencies (code, name_zh) values
  ('JPY', '日圓'),
  ('KRW', '韓元');

-- ---------------------------------------------------------------------------
-- 2. profiles：每個使用者一列，註冊時由 trigger 建立
-- ---------------------------------------------------------------------------
create table public.profiles (
  user_id           uuid primary key references auth.users (id) on delete cascade,
  email             text not null,
  email_enabled     boolean not null default true,
  -- 最近一次關閉 Email 通知的時間；重新開啟時清空
  email_disabled_at timestamptz,
  created_at        timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- 3. alert_rules：每人每個幣別一條規則
-- ---------------------------------------------------------------------------
create table public.alert_rules (
  id               bigint generated always as identity primary key,
  user_id          uuid not null default auth.uid()
                     references public.profiles (user_id) on delete cascade,
  currency         text not null references public.currencies (code),
  target           numeric(12, 6) not null check (target > 0 and target < 1000),
  -- null = 自動：目標價的 0.5%（設定頁的「進階設定」才能填）
  buffer           numeric(12, 6) check (buffer is null or (buffer >= 0 and buffer <= target * 0.1)),
  -- 排程實際使用的 buffer；跟著 target 自動重算，不能直接寫入
  effective_buffer numeric(12, 6) generated always as (coalesce(buffer, round(target * 0.005, 6))) stored,
  enabled          boolean not null default true,

  -- 以下 state_* 只有排程（service_role）能寫，對應單人版的 state.json
  state            text not null default 'armed' check (state in ('armed', 'triggered')),
  state_changed_at timestamptz,
  state_rate       numeric(12, 6),
  state_target     numeric(12, 6),

  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),

  -- 每人每個幣別只有一條規則；幣別有限，所以每人的規則數量也有上限
  unique (user_id, currency)
);

-- ---------------------------------------------------------------------------
-- 4. notifications：通知紀錄（只有排程能新增）
-- ---------------------------------------------------------------------------
create table public.notifications (
  id       bigint generated always as identity primary key,
  user_id  uuid not null references public.profiles (user_id) on delete cascade,
  rule_id  bigint references public.alert_rules (id) on delete set null,
  currency text not null references public.currencies (code),
  rate     numeric(12, 6) not null,
  target   numeric(12, 6) not null,
  sent_at  timestamptz not null default now()
);

create index notifications_user_sent_idx on public.notifications (user_id, sent_at desc);
create index notifications_rule_idx on public.notifications (rule_id);

-- ---------------------------------------------------------------------------
-- 5. Triggers
-- ---------------------------------------------------------------------------

-- 5a. 註冊（Google 第一次登入）時建立 profile；email 變更時同步
create function public.handle_auth_user_change()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if tg_op = 'INSERT' then
    insert into public.profiles (user_id, email) values (new.id, new.email);
  elsif new.email is distinct from old.email then
    update public.profiles set email = new.email where user_id = new.id;
  end if;
  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_auth_user_change();

create trigger on_auth_user_email_changed
  after update of email on auth.users
  for each row execute function public.handle_auth_user_change();

-- 5b. Email 通知開關
--   關閉：記錄時間。這段期間排程不判斷、不更新狀態（排程那一步會測試）。
--   重新開啟：這個人的所有規則重設為等待中，不補寄關閉期間的通知。
create function public.handle_email_enabled_change()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.email_enabled = old.email_enabled then
    return new;
  end if;

  if new.email_enabled then
    new.email_disabled_at := null;
    update public.alert_rules
       set state = 'armed', state_changed_at = now(), state_rate = null, state_target = null
     where user_id = new.user_id;
  else
    new.email_disabled_at := now();
  end if;
  return new;
end;
$$;

create trigger on_email_enabled_change
  before update of email_enabled on public.profiles
  for each row execute function public.handle_email_enabled_change();

-- 5c. 規則：更新時間；停用後重新啟用 → 重設為等待中（理由同 5b）
create function public.handle_alert_rule_update()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  if old.enabled = false and new.enabled = true then
    new.state := 'armed';
    new.state_changed_at := now();
    new.state_rate := null;
    new.state_target := null;
  end if;
  return new;
end;
$$;

create trigger on_alert_rule_update
  before update on public.alert_rules
  for each row execute function public.handle_alert_rule_update();

-- trigger 函式不給任何人當 RPC 呼叫
revoke all on function public.handle_auth_user_change() from public, anon, authenticated;
revoke all on function public.handle_email_enabled_change() from public, anon, authenticated;
revoke all on function public.handle_alert_rule_update() from public, anon, authenticated;

-- ---------------------------------------------------------------------------
-- 6. 權限：先全部收回，再逐一開放
-- ---------------------------------------------------------------------------
revoke all on public.currencies, public.profiles, public.alert_rules, public.notifications
  from anon, authenticated;
grant all on public.currencies, public.profiles, public.alert_rules, public.notifications
  to service_role;

-- currencies：誰都能讀
grant select on public.currencies to anon, authenticated;

-- profiles：登入者能讀自己的；只能改 email_enabled（不能改 email，避免把通知導到別人信箱）
grant select on public.profiles to authenticated;
grant update (email_enabled) on public.profiles to authenticated;

-- alert_rules：登入者能讀、刪自己的；新增 / 修改只限設定欄位，state_* 只有排程能寫
grant select, delete on public.alert_rules to authenticated;
grant insert (currency, target, buffer, enabled) on public.alert_rules to authenticated;
grant update (target, buffer, enabled) on public.alert_rules to authenticated;

-- notifications：登入者只能讀自己的
grant select on public.notifications to authenticated;

-- ---------------------------------------------------------------------------
-- 7. RLS
-- ---------------------------------------------------------------------------
alter table public.currencies    enable row level security;
alter table public.profiles      enable row level security;
alter table public.alert_rules   enable row level security;
alter table public.notifications enable row level security;

create policy "currencies are public"
  on public.currencies for select
  to anon, authenticated
  using (true);

create policy "read own profile"
  on public.profiles for select
  to authenticated
  using (user_id = (select auth.uid()));

create policy "update own profile"
  on public.profiles for update
  to authenticated
  using (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));

create policy "read own rules"
  on public.alert_rules for select
  to authenticated
  using (user_id = (select auth.uid()));

create policy "insert own rules"
  on public.alert_rules for insert
  to authenticated
  with check (user_id = (select auth.uid()));

create policy "update own rules"
  on public.alert_rules for update
  to authenticated
  using (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));

create policy "delete own rules"
  on public.alert_rules for delete
  to authenticated
  using (user_id = (select auth.uid()));

create policy "read own notifications"
  on public.notifications for select
  to authenticated
  using (user_id = (select auth.uid()));
