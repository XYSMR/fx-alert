-- 單人版 → 多人版的搬家用：把某個使用者的規則和通知紀錄一次寫進去。
-- 只有 service_role 能呼叫（python -m fx_alert migrate-me，在自己的電腦執行）。
--
--   * 整個函式是同一個交易：任何一步失敗，什麼都不會寫入。
--   * 這個人已經有規則 → 拒絕（避免不小心執行兩次）；
--     p_replace = true 時，先在同一個交易裡刪掉他的通知紀錄和規則，再重新寫入，
--     所以 --replace 執行幾次，結果都一樣，通知紀錄不會重複。
--   * p_rules：[{currency, target, buffer, state, state_changed_at, state_rate, state_target}]
--     p_notifications：[{currency, rate, target, sent_at}]（依幣別對到剛寫入的規則）
--
-- 回傳 {"rules": 寫入幾條, "notifications": 寫入幾筆}

create function public.import_rules_for_user(
  p_user_id       uuid,
  p_rules         jsonb,
  p_notifications jsonb,
  p_replace       boolean default false
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_rules int;
  v_notes int;
begin
  if not exists (select 1 from public.profiles where user_id = p_user_id) then
    raise exception 'fx_alert: profile not found' using errcode = 'P0002';
  end if;

  if exists (select 1 from public.alert_rules where user_id = p_user_id) and not p_replace then
    raise exception 'fx_alert: user already has rules' using errcode = 'P0001';
  end if;

  if p_replace then
    delete from public.notifications where user_id = p_user_id;
    delete from public.alert_rules where user_id = p_user_id;
  end if;

  insert into public.alert_rules
    (user_id, currency, target, buffer, enabled, state, state_changed_at, state_rate, state_target)
  select p_user_id,
         r ->> 'currency',
         (r ->> 'target')::numeric,
         (r ->> 'buffer')::numeric,
         true,
         coalesce(r ->> 'state', 'armed'),
         (r ->> 'state_changed_at')::timestamptz,
         (r ->> 'state_rate')::numeric,
         (r ->> 'state_target')::numeric
    from jsonb_array_elements(p_rules) as r;
  get diagnostics v_rules = row_count;

  insert into public.notifications (user_id, rule_id, currency, rate, target, sent_at)
  select p_user_id,
         ar.id,
         n ->> 'currency',
         (n ->> 'rate')::numeric,
         (n ->> 'target')::numeric,
         (n ->> 'sent_at')::timestamptz
    from jsonb_array_elements(p_notifications) as n
    left join public.alert_rules ar
      on ar.user_id = p_user_id and ar.currency = n ->> 'currency';
  get diagnostics v_notes = row_count;

  return jsonb_build_object('rules', v_rules, 'notifications', v_notes);
end;
$$;

revoke all on function public.import_rules_for_user(uuid, jsonb, jsonb, boolean)
  from public, anon, authenticated;
grant execute on function public.import_rules_for_user(uuid, jsonb, jsonb, boolean)
  to service_role;
