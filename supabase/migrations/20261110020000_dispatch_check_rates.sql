-- 外部觸發：用 pg_cron 定時呼叫 GitHub API 的 workflow_dispatch，補 GitHub 排程的延遲／丟棄。
--
-- 為什麼：GitHub Actions 的 schedule 是盡力而為，負載高時會延遲，甚至直接丟掉（不留紀錄）。
-- pg_cron 在資料庫裡準時執行，透過 pg_net 呼叫 GitHub API 觸發 workflow；GitHub 自己的 cron 保留當備援。
--
-- 設定（每個專案各自在 Supabase 後台的 Vault 頁面新增，**不要**寫進 repo 或 SQL Editor）：
--   github_dispatch_token     fine-grained PAT：只限 XYSMR/fx-alert、只開 Actions: Read and write、有到期日
--   github_dispatch_workflow  要觸發的 workflow 檔名：正式專案 check-rates.yml；staging 用 staging-check.yml
-- 兩個都沒設定時，函式什麼都不做（例如還沒啟用、或只在 staging 驗證時的正式專案）。
--
-- token 只存在 Vault：pg_cron 的指令只有 `select private.dispatch_check_rates()`，
-- 所以 cron.job、cron.job_run_details 都看不到 token。

create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net with schema extensions;

create schema if not exists private;
revoke all on schema private from public;
comment on schema private is 'fx-alert：只給資料庫內部（pg_cron）使用，不對 API 開放';

-- 每個 workflow 送出的 inputs（白名單）。GitHub 收到 workflow 沒定義的 input 會回 422，
-- 所以這裡只能送 workflow 檔裡有的 input；tests/test_supabase_config.py 會比對兩邊。
--   check-rates.yml：force 一定要明確送 "false"。workflow 裡 force 的預設是 true（給手動執行用），
--                    沒送的話就會略過「台灣時間 09:00–16:30」的時段判斷。dry_run 也明確送 "false"。
--   staging-check.yml：send 送 "false"（只讀，不寄信、不寫入 staging）。
create or replace function private.dispatch_inputs(p_workflow text)
returns jsonb
language sql
immutable
set search_path = ''
as $$
  select case p_workflow
    when 'check-rates.yml' then '{"force": "false", "dry_run": "false"}'::jsonb
    when 'staging-check.yml' then '{"send": "false"}'::jsonb
  end
$$;

-- 送出一次 workflow_dispatch。回傳 pg_net 的 request id；沒設定時回傳 null。
-- pg_net 是非同步、送出就不管：結果要看 net._http_response（成功是 204，保留 6 小時），失敗不會重試。
create or replace function private.dispatch_check_rates()
returns bigint
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_token text;
  v_workflow text;
  v_inputs jsonb;
begin
  -- 從後台貼上時可能多了空白或換行：一律去掉
  select btrim(decrypted_secret, E' \t\r\n') into v_token
    from vault.decrypted_secrets where name = 'github_dispatch_token';
  select btrim(decrypted_secret, E' \t\r\n') into v_workflow
    from vault.decrypted_secrets where name = 'github_dispatch_workflow';

  if coalesce(v_token, '') = '' or coalesce(v_workflow, '') = '' then
    raise notice 'fx_alert: dispatch not configured, skipped';
    return null;
  end if;

  v_inputs := private.dispatch_inputs(v_workflow);
  if v_inputs is null then
    raise exception 'fx_alert: workflow % is not allowed', v_workflow
      using errcode = '22023';
  end if;

  return net.http_post(
    url := 'https://api.github.com/repos/XYSMR/fx-alert/actions/workflows/'
           || v_workflow || '/dispatches',
    body := jsonb_build_object('ref', 'main', 'inputs', v_inputs),
    headers := jsonb_build_object(
      'Accept', 'application/vnd.github+json',
      'Authorization', 'Bearer ' || v_token,
      'Content-Type', 'application/json',
      'User-Agent', 'fx-alert-pg-cron',
      'X-GitHub-Api-Version', '2022-11-28'
    ),
    timeout_milliseconds := 10000
  );
end;
$$;

revoke all on function private.dispatch_inputs(text) from public, anon, authenticated, service_role;
revoke all on function private.dispatch_check_rates() from public, anon, authenticated, service_role;

-- 排程（pg_cron 用 UTC；台灣 = UTC+8）。比 GitHub cron（:07、:37）早 2 分鐘，GitHub 的留著當備援。
--   '5,35 1-7 * * 1-5'  ＝ 台灣平日 09:05、09:35、10:05 … 15:05、15:35
--   '5 8 * * 1-5'       ＝ 台灣平日 16:05
-- cron.schedule 用同一個名稱會更新原本的排程，所以這個 migration 重複執行也不會多出工作。
select cron.schedule('fx-alert-dispatch', '5,35 1-7 * * 1-5', 'select private.dispatch_check_rates()');
select cron.schedule('fx-alert-dispatch-1605', '5 8 * * 1-5', 'select private.dispatch_check_rates()');
