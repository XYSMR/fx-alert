-- 排程寄信成功後呼叫：把規則改成「已通知」＋寫一筆通知紀錄，兩件事在同一個交易裡完成。
-- 只有 service_role（排程的 secret key）能呼叫。
--
-- 回傳新通知紀錄的 id；規則在這段期間被使用者刪掉的話回傳 null（沒有東西可以更新）。

create function public.record_alert(
  p_rule_id bigint,
  p_rate    numeric,
  p_target  numeric,
  p_sent_at timestamptz
)
returns bigint
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_user_id  uuid;
  v_currency text;
  v_id       bigint;
begin
  update public.alert_rules
     set state            = 'triggered',
         state_changed_at = p_sent_at,
         state_rate       = p_rate,
         state_target     = p_target
   where id = p_rule_id
  returning user_id, currency into v_user_id, v_currency;

  if not found then
    return null;
  end if;

  insert into public.notifications (user_id, rule_id, currency, rate, target, sent_at)
  values (v_user_id, p_rule_id, v_currency, p_rate, p_target, p_sent_at)
  returning id into v_id;

  return v_id;
end;
$$;

revoke all on function public.record_alert(bigint, numeric, numeric, timestamptz)
  from public, anon, authenticated;
grant execute on function public.record_alert(bigint, numeric, numeric, timestamptz)
  to service_role;
