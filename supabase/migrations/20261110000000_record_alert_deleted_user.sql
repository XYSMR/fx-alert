-- record_alert：使用者在排程檢查期間刪除帳號時，安靜地回傳 null（不是錯誤）。
--
-- 會發生兩種情況：
--   1. 規則已經被連帶刪除 → update 找不到 → 回傳 null（原本就是這樣）
--   2. 刪除剛好和這個函式同時進行，insert 通知紀錄時 user_id 已不存在
--      → foreign_key_violation → 這次的 update 一起回復，回傳 null
-- 排程看到 null 只會記一行 log、略過這個人，不影響其他人，也不讓整次排程失敗。

create or replace function public.record_alert(
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
exception
  when foreign_key_violation then
    return null; -- 帳號在這段期間被刪除；上面的 update 會一起回復
end;
$$;

-- create or replace 會保留原本的權限；再寫一次，讓這個檔案單獨看也清楚
revoke all on function public.record_alert(bigint, numeric, numeric, timestamptz)
  from public, anon, authenticated;
grant execute on function public.record_alert(bigint, numeric, numeric, timestamptz)
  to service_role;
