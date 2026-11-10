-- pg_cron 觸發時多送 source="pg_cron"，check-rates 的去重判斷（gate job）用它分辨「自動觸發」和「手動執行」。
--
-- ⚠ 套用順序：先合併加上 source input 的 check-rates.yml（GitHub 收到 workflow 沒定義的 input 會回 422），
--   再套用這個 migration。staging-check.yml 沒有 source，所以不變。
create or replace function private.dispatch_inputs(p_workflow text)
returns jsonb
language sql
immutable
set search_path = ''
as $$
  select case p_workflow
    when 'check-rates.yml' then '{"force": "false", "dry_run": "false", "source": "pg_cron"}'::jsonb
    when 'staging-check.yml' then '{"send": "false"}'::jsonb
  end
$$;

revoke all on function private.dispatch_inputs(text) from public, anon, authenticated, service_role;
