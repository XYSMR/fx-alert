-- 出國換匯計畫：旅程、換匯紀錄、提醒紀錄（規則在 src/fx_alert/trip_rules.py）
--
-- 原則和 20261109000000 一樣：先收回預設權限，只開放需要的「表 + 欄位」，三張表都開 RLS。
--   * 使用者（authenticated）只能看、改自己的旅程和換匯紀錄；提醒紀錄只能看。
--   * 排程（service_role）讀 active_trips、寫 trip_notifications。
--   * 刪除使用者 → profiles → trips → trip_exchanges / trip_notifications 全部連帶刪除。
--   * 「今天」一律是台灣時間的日期。

-- ---------------------------------------------------------------------------
-- 1. trips：一趟旅程 = 一種幣別 + 出發日 + 需要的外幣金額
-- ---------------------------------------------------------------------------
create table public.trips (
  id         bigint generated always as identity primary key,
  user_id    uuid not null default auth.uid()
               references public.profiles (user_id) on delete cascade,
  currency   text not null references public.currencies (code),
  departure  date not null,
  -- 需要的外幣金額（例如 50000 日圓）；越南盾、印尼盾的金額很大，所以上限放寬
  amount     numeric(16, 2) not null check (amount > 0 and amount < 1e12),
  -- 自己看的名稱（例如「東京」），可以不填
  name       text check (name is null or char_length(name) between 1 and 30),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  -- 給 trip_exchanges 的複合外鍵用：換匯紀錄的 user_id 一定和旅程相同
  unique (id, user_id)
);

create index trips_user_departure_idx on public.trips (user_id, departure);

-- ---------------------------------------------------------------------------
-- 2. trip_exchanges：已經換了多少（使用者自己記錄）
-- ---------------------------------------------------------------------------
create table public.trip_exchanges (
  id           bigint generated always as identity primary key,
  trip_id      bigint not null,
  user_id      uuid not null default auth.uid(),
  amount       numeric(16, 2) not null check (amount > 0 and amount < 1e12),
  -- 換到的匯率（1 外幣 = 幾元台幣），可以不填
  rate         numeric(12, 4) check (rate is null or (rate > 0 and rate < 1000)),
  exchanged_on date not null default (now() at time zone 'Asia/Taipei')::date,
  created_at   timestamptz not null default now(),
  foreign key (trip_id, user_id) references public.trips (id, user_id) on delete cascade
);

create index trip_exchanges_trip_idx on public.trip_exchanges (trip_id);

-- ---------------------------------------------------------------------------
-- 3. trip_notifications：寄過的提醒（只有排程能新增）
-- ---------------------------------------------------------------------------
create table public.trip_notifications (
  id          bigint generated always as identity primary key,
  trip_id     bigint not null,
  user_id     uuid not null,
  kind        text not null
                check (kind in ('cheap_far', 'near_start', 'cheap_near', 'final', 'last_call')),
  sent_on     date not null,  -- 台灣時間的日期（規則：一天最多一封）
  sent_at     timestamptz not null default now(),
  rate        numeric(12, 4),  -- 寄信當時的現金賣出價
  rank        int check (rank is null or rank >= 1),  -- 近 30 天第幾低（價格提醒才有）
  window_days int check (window_days is null or window_days >= 1),  -- 近 30 天有幾個營業日
  days_left   int not null check (days_left >= 0),
  remaining   numeric(16, 2) not null,
  foreign key (trip_id, user_id) references public.trips (id, user_id) on delete cascade,
  unique (trip_id, sent_on)
);

-- 固定日期的提醒每趟只寄一次（排程重跑或兩個排程同時跑也不會重複）
create unique index trip_notifications_fixed_once
  on public.trip_notifications (trip_id, kind)
  where kind in ('near_start', 'final', 'last_call');

create index trip_notifications_user_idx on public.trip_notifications (user_id, sent_at desc);

-- ---------------------------------------------------------------------------
-- 4. Triggers
-- ---------------------------------------------------------------------------

-- 4a. 新增旅程：出發日要在明天到一年內；每人最多 5 趟「還沒出發」的旅程
create function public.check_trip_insert()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_today date := (now() at time zone 'Asia/Taipei')::date;
  v_count int;
begin
  if new.departure <= v_today or new.departure > v_today + 365 then
    raise exception '出發日要在明天到一年內' using errcode = '23514';
  end if;

  -- 同一個人同時新增兩趟時排隊，避免兩邊都數到 4 趟
  perform 1 from public.profiles where user_id = new.user_id for update;
  select count(*) into v_count
    from public.trips
   where user_id = new.user_id and departure > v_today;
  if v_count >= 5 then
    raise exception '每人最多 5 趟還沒出發的旅程' using errcode = '23514';
  end if;
  return new;
end;
$$;

create trigger on_trip_insert
  before insert on public.trips
  for each row execute function public.check_trip_insert();

create function public.handle_trip_update()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

create trigger on_trip_update
  before update on public.trips
  for each row execute function public.handle_trip_update();

-- 4b. 換匯紀錄：每趟最多 50 筆（防止灌資料）
create function public.check_trip_exchange_insert()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  -- 只鎖、只數自己的旅程（別人的旅程 id 交給外鍵擋下來，這裡不透露任何資訊）
  perform 1 from public.trips where id = new.trip_id and user_id = new.user_id for update;
  if (select count(*) from public.trip_exchanges
       where trip_id = new.trip_id and user_id = new.user_id) >= 50 then
    raise exception '每趟旅程最多 50 筆換匯紀錄' using errcode = '23514';
  end if;
  return new;
end;
$$;

create trigger on_trip_exchange_insert
  before insert on public.trip_exchanges
  for each row execute function public.check_trip_exchange_insert();

revoke all on function public.check_trip_insert() from public, anon, authenticated;
revoke all on function public.handle_trip_update() from public, anon, authenticated;
revoke all on function public.check_trip_exchange_insert() from public, anon, authenticated;

-- ---------------------------------------------------------------------------
-- 5. 權限：先全部收回，再逐一開放
-- ---------------------------------------------------------------------------
revoke all on public.trips, public.trip_exchanges, public.trip_notifications
  from anon, authenticated;
grant all on public.trips, public.trip_exchanges, public.trip_notifications to service_role;

-- trips：出發日、幣別建立後不能改（提醒的日期都是從出發日算的；要改就刪掉重建）
grant select, delete on public.trips to authenticated;
grant insert (currency, departure, amount, name) on public.trips to authenticated;
grant update (amount, name) on public.trips to authenticated;

grant select, delete on public.trip_exchanges to authenticated;
grant insert (trip_id, amount, rate, exchanged_on) on public.trip_exchanges to authenticated;
grant update (amount, rate, exchanged_on) on public.trip_exchanges to authenticated;

grant select on public.trip_notifications to authenticated;

-- ---------------------------------------------------------------------------
-- 6. RLS
-- ---------------------------------------------------------------------------
alter table public.trips              enable row level security;
alter table public.trip_exchanges     enable row level security;
alter table public.trip_notifications enable row level security;

create policy "read own trips" on public.trips for select to authenticated
  using (user_id = (select auth.uid()));
create policy "insert own trips" on public.trips for insert to authenticated
  with check (user_id = (select auth.uid()));
create policy "update own trips" on public.trips for update to authenticated
  using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
create policy "delete own trips" on public.trips for delete to authenticated
  using (user_id = (select auth.uid()));

create policy "read own exchanges" on public.trip_exchanges for select to authenticated
  using (user_id = (select auth.uid()));
create policy "insert own exchanges" on public.trip_exchanges for insert to authenticated
  with check (user_id = (select auth.uid()));
create policy "update own exchanges" on public.trip_exchanges for update to authenticated
  using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
create policy "delete own exchanges" on public.trip_exchanges for delete to authenticated
  using (user_id = (select auth.uid()));

create policy "read own trip notifications" on public.trip_notifications for select
  to authenticated using (user_id = (select auth.uid()));

-- ---------------------------------------------------------------------------
-- 7. Views
-- ---------------------------------------------------------------------------

-- 7a. 旅程進度頁用：已換多少、還剩多少、最近一封提醒。security_invoker → 只看得到自己的
create view public.trip_progress
with (security_invoker = true)
as
select
  t.id,
  t.user_id,
  t.currency,
  t.departure,
  t.amount,
  t.name,
  t.created_at,
  coalesce(x.exchanged, 0)                       as exchanged,
  greatest(t.amount - coalesce(x.exchanged, 0), 0) as remaining,
  x.count                                        as exchange_count,
  n.kind                                         as last_kind,
  n.sent_at                                      as last_sent_at
from public.trips t
left join lateral (
  select sum(e.amount) as exchanged, count(*)::int as count
    from public.trip_exchanges e
   where e.trip_id = t.id
) x on true
left join lateral (
  select tn.kind, tn.sent_at
    from public.trip_notifications tn
   where tn.trip_id = t.id
   order by tn.sent_at desc
   limit 1
) n on true;

revoke all on public.trip_progress from public, anon, authenticated;
grant select on public.trip_progress to authenticated, service_role;

-- 7b. 排程用：還沒出發、還沒換完、Email 通知開著的旅程，附上已寄過的提醒
create view public.active_trips
with (security_invoker = true)
as
select
  p.id,
  p.user_id,
  p.currency,
  p.departure,
  p.amount,
  p.remaining,
  (p.created_at at time zone 'Asia/Taipei')::date as created_on,
  pr.email,
  pr.notify_start,
  pr.notify_end,
  coalesce(
    (select jsonb_agg(jsonb_build_object('kind', tn.kind, 'on', tn.sent_on) order by tn.sent_on)
       from public.trip_notifications tn
      where tn.trip_id = p.id),
    '[]'::jsonb
  ) as sent
from public.trip_progress p
join public.profiles pr on pr.user_id = p.user_id
where p.departure > (now() at time zone 'Asia/Taipei')::date
  and p.remaining > 0
  and pr.email_enabled;

revoke all on public.active_trips from public, anon, authenticated;
grant select on public.active_trips to service_role;
