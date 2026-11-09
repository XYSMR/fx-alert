-- 排程每次要檢查的規則：規則啟用、而且主人的 Email 通知開著。
-- Email 關閉的人根本不會出現在這裡，所以排程不判斷、也不更新他們的狀態。
-- 只有 service_role（排程的 secret key）能讀；一般使用者讀自己的規則請用 alert_rules。

create view public.active_rules
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
  p.email
from public.alert_rules r
join public.profiles p on p.user_id = r.user_id
where r.enabled
  and p.email_enabled;

revoke all on public.active_rules from public, anon, authenticated;
grant select on public.active_rules to service_role;
