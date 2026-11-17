-- 20261116000000_currency_catalog 確實已套用（staging 在 SQL Editor 手動套用）
select 'public.currencies.sort_order' as item, exists (
  select 1 from information_schema.columns
  where table_schema = 'public' and table_name = 'currencies' and column_name = 'sort_order') as ok
union all
select 'public.currencies.popular_rank', exists (
  select 1 from information_schema.columns
  where table_schema = 'public' and table_name = 'currencies' and column_name = 'popular_rank')
union all
select '18 種幣別', (select count(*) = 18 from public.currencies)
union all
select 'public.alert_rules.effective_buffer', exists (
  select 1 from information_schema.columns
  where table_schema = 'public' and table_name = 'alert_rules' and column_name = 'effective_buffer')
union all
select 'view public.active_rules', to_regclass('public.active_rules') is not null
