-- 支援兆豐牌告上「有現金賣出價」的全部 18 種幣別，並把價格對齊牌告的報價單位 0.0001
--
-- 內容和 src/fx_alert/currencies.py 一致（tests/db/test_currency_catalog.py 會比對）。
-- 南非幣 ZAR、瑞典幣 SEK 兆豐只有即期匯率，不支援。
--
-- 套用前先在 SQL Editor 確認既有規則都符合新的限制（應該回傳 0 列；有資料就先修正，否則整個 migration 會失敗、什麼都不改）：
--   select id, currency, target, buffer from public.alert_rules
--   where target <> round(target, 4)
--      or (buffer is not null and (buffer < 0.0001 or buffer <> round(buffer, 4)
--                                  or buffer > greatest(target * 0.1, 0.0001)));

-- ---------------------------------------------------------------------------
-- 1. currencies：18 種幣別、牌告順序、下拉選單「常用」的順序
-- ---------------------------------------------------------------------------
alter table public.currencies
  add column sort_order   int,
  add column popular_rank int;

insert into public.currencies (code, name_zh, sort_order, popular_rank) values
  ('USD', '美元', 1, 3),
  ('HKD', '港幣', 2, 6),
  ('GBP', '英鎊', 3, null),
  ('JPY', '日圓', 4, 1),
  ('AUD', '澳幣', 5, null),
  ('CAD', '加拿大幣', 6, null),
  ('SGD', '新加坡幣', 7, 8),
  ('CHF', '瑞士法郎', 8, null),
  ('THB', '泰銖', 9, 7),
  ('NZD', '紐西蘭幣', 10, null),
  ('EUR', '歐元', 11, 4),
  ('KRW', '韓元', 12, 2),
  ('MYR', '馬來西亞幣', 13, null),
  ('IDR', '印尼盾', 14, null),
  ('PHP', '菲律賓比索', 15, null),
  ('MOP', '澳門幣', 16, null),
  ('VND', '越南盾', 17, null),
  ('CNY', '人民幣', 18, 5)
on conflict (code) do update
  set name_zh      = excluded.name_zh,
      sort_order   = excluded.sort_order,
      popular_rank = excluded.popular_rank;

alter table public.currencies
  alter column sort_order set not null,
  add constraint currencies_sort_order_key unique (sort_order),
  add constraint currencies_popular_rank_key unique (popular_rank);

-- ---------------------------------------------------------------------------
-- 2. 價格對齊報價單位 0.0001（兆豐牌告每個幣別都是小數 4 位）
--    * 目標價最多 4 位小數
--    * 回升門檻至少 0.0001、最多 4 位小數；上限是目標價的 10%，但至少允許 0.0001
--    * 自動回升門檻：目標價的 0.5% 四捨五入到 4 位，至少 0.0001
--      （原本是 round(target * 0.005, 6)：越南盾目標 0.0015 會變成 0.0000075，
--        回升價格出現 0.0015075 這種牌告上不可能出現的數字）
-- ---------------------------------------------------------------------------
-- 原本 buffer 的 check 是建表時自動命名的（目前是 alert_rules_check）；依內容找出來拿掉，不依賴名稱
do $$
declare
  c text;
begin
  for c in
    select conname from pg_constraint
    where conrelid = 'public.alert_rules'::regclass
      and contype = 'c'
      and pg_get_constraintdef(oid) like '%buffer%'
  loop
    execute format('alter table public.alert_rules drop constraint %I', c);
  end loop;
end
$$;

alter table public.alert_rules
  add constraint alert_rules_target_on_tick check (target = round(target, 4)),
  add constraint alert_rules_buffer_check check (
    buffer is null
    or (buffer >= 0.0001
        and buffer = round(buffer, 4)
        and buffer <= greatest(target * 0.1, 0.0001))
  );

-- generated column 不能直接改公式：先拿掉依賴它的 view，重建欄位，再照原樣重建 view
drop view public.active_rules;

alter table public.alert_rules drop column effective_buffer;
alter table public.alert_rules
  add column effective_buffer numeric(12, 6)
    generated always as (coalesce(buffer, greatest(round(target * 0.005, 4), 0.0001))) stored;

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
