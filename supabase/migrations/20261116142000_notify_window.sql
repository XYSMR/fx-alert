-- 通知時段：每個人可以設定「只在這段時間內寄到價通知」（台灣時間）。
--
-- 規則（main.py）：檢查時已到價、但不在時段內 → 不寄、狀態不變（還是等待中）；
-- 時段開始後的第一次檢查會「重新判斷」：那時匯率還在目標以下才寄，已經回升就繼續等待。
-- 不會補寄夜裡曾經到價的通知。
--
-- * 時間一律是台灣時間（Asia/Taipei），只能是整點或半點（檢查每 30 分鐘一次）。
-- * 開始 < 結束：當天的時段（預設 08:00–22:00）。
-- * 開始 > 結束：跨午夜（例如 22:00–07:00）。
-- * 開始 = 結束：全天都寄。

alter table public.profiles
  add column if not exists notify_start time not null default '08:00',
  add column if not exists notify_end   time not null default '22:00';

alter table public.profiles drop constraint if exists profiles_notify_window_half_hour;
alter table public.profiles add constraint profiles_notify_window_half_hour check (
  date_part('second', notify_start) = 0 and date_part('minute', notify_start) in (0, 30)
  and date_part('second', notify_end) = 0 and date_part('minute', notify_end) in (0, 30)
);

comment on column public.profiles.notify_start is '通知時段開始（台灣時間，整點或半點）';
comment on column public.profiles.notify_end is
  '通知時段結束（台灣時間，不含）；比開始早 = 跨午夜；和開始相同 = 全天';

-- 登入者可以改自己的通知時段（email 仍然不能改）
grant update (email_enabled, notify_start, notify_end) on public.profiles to authenticated;

-- 排程讀規則時一併讀通知時段。欄位加在最後，create or replace 不影響既有欄位和權限
create or replace view public.active_rules
with (security_invoker = true)
as
select
  r.id,
  r.user_id,
  r.currency,
  r.target,
  r.effective_buffer,
  r.state,
  r.state_target,
  p.email,
  p.notify_start,
  p.notify_end
from public.alert_rules r
join public.profiles p on p.user_id = r.user_id
where r.enabled
  and p.email_enabled;

revoke all on public.active_rules from public, anon, authenticated;
grant select on public.active_rules to service_role;
