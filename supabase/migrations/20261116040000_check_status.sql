-- 最後一次檢查的結果（只有一列）。
--
-- 原本每次檢查都把 last_check.json commit 到 state 分支、再部署一次 GitHub Pages，
-- 只是為了更新「最後檢查時間」。改成寫到這張表之後：
--   * check-rates 每次檢查都 upsert 這一列（secret key，繞過 RLS）
--   * 去重（gate）和公開頁面的狀態列用 publishable key 讀它
--   * state 分支和 Pages 只在走勢、狀態、通知紀錄真的有變時才 commit／部署
--
-- 公開資訊：只有時間、成功與否、錯誤「數量」，不放錯誤訊息（可能含 email）。
-- 讀不到這張表時（例如專案被暫停），gate 照常檢查、頁面退回用 data.json 的產生時間。

create table if not exists public.check_status (
  id smallint primary key default 1 check (id = 1),
  checked_at timestamptz not null,
  ok boolean not null,
  errors integer not null default 0 check (errors >= 0),
  updated_at timestamptz not null default now()
);

comment on table public.check_status is
  '最後一次檢查的結果（只有一列）。任何人可讀；只有 check-rates（secret key）可寫。';

alter table public.check_status enable row level security;

drop policy if exists "check status is public" on public.check_status;
create policy "check status is public"
  on public.check_status for select
  to anon, authenticated
  using (true);

-- 只開放讀取；寫入只靠 secret key（service_role 繞過 RLS）
revoke all on public.check_status from anon, authenticated;
grant select on public.check_status to anon, authenticated;
grant select, insert, update on public.check_status to service_role;
