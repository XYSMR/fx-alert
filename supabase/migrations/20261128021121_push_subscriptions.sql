-- 手機推播（Web Push）的訂閱：每台裝置一列（設計見 docs/web-push.md，推播 PR 2／5）。
--
-- ⚠ 這個 migration 只「新增」：一張資料表、三個函式、一個 trigger，和一筆預設關閉的 web_push flag。
--   不修改任何現有的資料表、函式、policy 或 flag，所以 A10 Demo 用到的 trip_plan、quick_target、
--   Email 提醒都不受影響（tests/test_push_migration_scope.py 檢查這件事）。
--
-- 權限（RLS）：
--   * 使用者只能看、改名稱、刪除自己的裝置。不能直接 insert：一律透過 save_push_subscription()，
--     它會檢查 web_push flag。flag 關閉時不能新增，但可以刪除（關掉功能不刪使用者的資料）。
--   * 加密金鑰（p256dh、auth）使用者讀不到，只有排程（service_role）讀得到。
--   * 刪除帳號：auth.users → profiles → push_subscriptions 連帶刪除。
--
-- 推播網址白名單（防 SSRF：排程會對這個網址發送請求）：
--   * 只接受 https、主機名稱不能帶帳號或 port、不能有空白和控制字元
--   * 主機名稱用「結尾比對」，而且必須比對在網域的邊界上：
--       *.push.apple.com、fcm.googleapis.com、updates.push.services.mozilla.com、*.notify.windows.com
--   * 寄送端（PR 4）會用同一份規則再檢查一次，並且不跟隨轉址。
--   * 測試案例：tests/fixtures/push_endpoints.json（資料庫和寄送端共用）

-- ---------------------------------------------------------------------------
-- 1. 白名單檢查
-- ---------------------------------------------------------------------------
create function public.is_allowed_push_endpoint(p_endpoint text)
returns boolean
language sql
immutable
set search_path = ''
as $$
  select coalesce(
    p_endpoint is not null
    and char_length(p_endpoint) <= 2048
    and p_endpoint !~ '[[:space:][:cntrl:]\\]'
    -- https:// + 主機名稱（只有小寫英數、點、連字號 → 不能有 @ 帳號、: port）+ 一定要有路徑
    and p_endpoint ~ '^https://[a-z0-9.-]+/'
    and (
      substring(p_endpoint from '^https://([a-z0-9.-]+)/') in (
        'fcm.googleapis.com',
        'updates.push.services.mozilla.com'
      )
      or substring(p_endpoint from '^https://([a-z0-9.-]+)/') ~ '^([a-z0-9-]+\.)+push\.apple\.com$'
      or substring(p_endpoint from '^https://([a-z0-9.-]+)/') ~ '^([a-z0-9-]+\.)+notify\.windows\.com$'
    ),
    false
  );
$$;

revoke all on function public.is_allowed_push_endpoint(text) from public;
grant execute on function public.is_allowed_push_endpoint(text) to authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 2. push_subscriptions
-- ---------------------------------------------------------------------------
create table public.push_subscriptions (
  id              bigint generated always as identity primary key,
  user_id         uuid not null references public.profiles (user_id) on delete cascade,
  endpoint        text not null unique
                    constraint push_subscriptions_endpoint_allowed
                    check (public.is_allowed_push_endpoint(endpoint)),
  -- 瀏覽器給的加密金鑰（base64url）：p256dh 是 65 bytes、auth 是 16 bytes
  p256dh          text not null
                    constraint push_subscriptions_p256dh_format
                    check (p256dh ~ '^[A-Za-z0-9_-]{87}=?$'),
  auth            text not null
                    constraint push_subscriptions_auth_format
                    check (auth ~ '^[A-Za-z0-9_-]{22}(==)?$'),
  -- 使用者自己取的裝置名稱（例如「我的 iPhone」），可以不填
  label           text
                    constraint push_subscriptions_label_length
                    check (label is null or char_length(label) between 1 and 30),
  created_at      timestamptz not null default now(),
  -- 網頁每次打開都會重新同步（Apple 對失效的訂閱可能還是回 200，靠這欄找出很久沒出現的裝置）
  last_seen_at    timestamptz not null default now(),
  last_success_at timestamptz,
  fail_count      int not null default 0 check (fail_count >= 0)
);

create index push_subscriptions_user_idx on public.push_subscriptions (user_id);

comment on table public.push_subscriptions is
  '手機推播訂閱（每台裝置一列）。新增只能透過 save_push_subscription()；金鑰只有 service_role 讀得到。';

-- ---------------------------------------------------------------------------
-- 3. 每人最多 5 台裝置（新增，或裝置換成另一個帳號時檢查）
-- ---------------------------------------------------------------------------
create function public.check_push_subscription_limit()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_count int;
begin
  if tg_op = 'UPDATE' and new.user_id = old.user_id then
    return new;
  end if;
  -- insert ... on conflict 會先觸發這個 BEFORE INSERT，即使最後變成更新已有的那一列。
  -- 已經存在的裝置不算新增；如果它要換到另一個帳號，會在 BEFORE UPDATE（user_id 改變）時檢查。
  if tg_op = 'INSERT'
     and exists (select 1 from public.push_subscriptions where endpoint = new.endpoint) then
    return new;
  end if;
  -- 同一個人同時新增兩台時排隊，避免兩邊都數到 4 台
  perform 1 from public.profiles where user_id = new.user_id for update;
  select count(*) into v_count
    from public.push_subscriptions
   where user_id = new.user_id;
  if v_count >= 5 then
    raise exception '每人最多 5 台裝置接收推播' using errcode = '23514';
  end if;
  return new;
end;
$$;

revoke all on function public.check_push_subscription_limit() from public, anon, authenticated;

create trigger on_push_subscription_insert
  before insert or update of user_id on public.push_subscriptions
  for each row execute function public.check_push_subscription_limit();

-- ---------------------------------------------------------------------------
-- 4. save_push_subscription()：網頁訂閱、或每次打開時重新同步
--
--   * 沒登入、web_push 對這個人沒開 → 42501（PostgREST 回 403）
--   * 同一個推播網址已經存在：更新金鑰和 last_seen_at。
--     如果那一列屬於另一個帳號（同一台裝置換人登入），改成目前的帳號，名稱和成功紀錄清掉。
--     推播網址只有那台裝置的瀏覽器知道，所以拿得到網址就代表在用那台裝置。
--   * 回傳這台裝置的 id
-- ---------------------------------------------------------------------------
create function public.save_push_subscription(
  p_endpoint text,
  p_p256dh   text,
  p_auth     text,
  p_label    text default null
)
returns bigint
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_id  bigint;
begin
  if v_uid is null then
    raise exception '需要登入' using errcode = '42501';
  end if;
  if not public.is_feature_on('web_push') then
    raise exception '推播功能尚未開放' using errcode = '42501';
  end if;

  insert into public.push_subscriptions as s (user_id, endpoint, p256dh, auth, label)
  values (v_uid, p_endpoint, p_p256dh, p_auth, p_label)
  on conflict (endpoint) do update
     set p256dh          = excluded.p256dh,
         auth            = excluded.auth,
         last_seen_at    = now(),
         fail_count      = 0,
         label           = case when s.user_id = excluded.user_id
                                then coalesce(excluded.label, s.label)
                                else excluded.label end,
         last_success_at = case when s.user_id = excluded.user_id
                                then s.last_success_at end,
         user_id         = excluded.user_id
  returning id into v_id;
  return v_id;
end;
$$;

revoke all on function public.save_push_subscription(text, text, text, text) from public, anon;
grant execute on function public.save_push_subscription(text, text, text, text) to authenticated;

-- ---------------------------------------------------------------------------
-- 5. 權限：使用者看得到自己的裝置（不含金鑰）、只能改名稱、可以刪除
-- ---------------------------------------------------------------------------
revoke all on public.push_subscriptions from anon, authenticated;
grant select (id, endpoint, label, created_at, last_seen_at, last_success_at, fail_count)
  on public.push_subscriptions to authenticated;
grant update (label) on public.push_subscriptions to authenticated;
grant delete on public.push_subscriptions to authenticated;
grant all on public.push_subscriptions to service_role;

alter table public.push_subscriptions enable row level security;

create policy "select own push subscriptions" on public.push_subscriptions for select
  to authenticated using (user_id = (select auth.uid()));

create policy "rename own push subscriptions" on public.push_subscriptions for update
  to authenticated
  using (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));

create policy "delete own push subscriptions" on public.push_subscriptions for delete
  to authenticated using (user_id = (select auth.uid()));

-- ---------------------------------------------------------------------------
-- 6. web_push flag：預設關閉（連 admin、beta 都關）。正式專案等 12/15 A10 Demo 之後才開。
-- ---------------------------------------------------------------------------
insert into public.feature_flags (flag, enabled, rollout) values ('web_push', false, 0);
