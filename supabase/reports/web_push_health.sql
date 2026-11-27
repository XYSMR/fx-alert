-- 手機推播（web_push）的健康報表。rollout 期間每天看一次（docs/rollout-plan.md）。
-- 在 Supabase 的 SQL Editor 整段執行（postgres 身分）。不是 migration：部署 workflow 不會執行它。
--
-- 只輸出彙總數字：不輸出推播網址、金鑰、user_id 或 email，截圖可以直接貼到 PR 或群組。
-- result：PASS / FAIL 對照門檻；INFO 是參考值；NO DATA 是樣本不夠（少於 5 台裝置）。
--
-- 門檻
--   失敗中的裝置（fail_count > 0）比例 ≤ 20%：超過代表推播服務或金鑰有問題，考慮拉 kill switch
--   每人裝置數 ≤ 5、推播網址都在白名單內：資料庫已經擋住，這兩列應該永遠是 PASS
--
-- 整段強制唯讀（和 hdd_metrics.sql 相同）：就算多貼了一行寫入，資料庫也會拒絕。
set transaction read only;

with subs as (
  select s.*,
         substring(s.endpoint from '^https://([a-z0-9.-]+)/') as host
    from public.push_subscriptions s
),
totals as (
  select count(*)                                                              as devices,
         count(distinct user_id)                                               as users,
         count(*) filter (where fail_count > 0)                                as failing,
         count(*) filter (where last_success_at > now() - interval '24 hours') as ok_24h,
         count(*) filter (where last_success_at is null
                            and created_at < now() - interval '3 days')        as never_ok,
         count(*) filter (where last_seen_at < now() - interval '30 days')     as stale,
         count(*) filter (where not public.is_allowed_push_endpoint(endpoint)) as not_allowed
    from subs
),
per_user as (
  select coalesce(max(n), 0) as max_devices
    from (select count(*) as n from subs group by user_id) t
),
flag as (
  select coalesce(max(case when enabled then 'on' else 'off' end || ' / ' || rollout), '（沒有這個 flag）') as state
    from public.feature_flags
   where flag = 'web_push'
),
rows as (
  select 1 as ord, 'web_push flag（enabled / rollout）' as metric, (select state from flag) as value,
         '' as target, 'INFO' as result
  union all
  select 2, '接收推播的裝置', devices::text, '', 'INFO' from totals
  union all
  select 3, '有裝置的使用者', users::text, '', 'INFO' from totals
  union all
  select 4, '裝置（Apple / Google / Mozilla / Windows）',
         (select count(*) filter (where host like '%.push.apple.com') || ' / ' ||
                 count(*) filter (where host = 'fcm.googleapis.com') || ' / ' ||
                 count(*) filter (where host = 'updates.push.services.mozilla.com') || ' / ' ||
                 count(*) filter (where host like '%.notify.windows.com')
            from subs),
         '', 'INFO'
  union all
  select 5, '最近 24 小時送達過的裝置', ok_24h::text, '', 'INFO' from totals
  union all
  select 6, '失敗中的裝置比例',
         case when devices = 0 then '—'
              else round(100.0 * failing / devices, 1) || '%（' || failing || ' 台）' end,
         '≤ 20%',
         case when devices < 5 then 'NO DATA'
              when failing * 100 <= devices * 20 then 'PASS'
              else 'FAIL' end
    from totals
  union all
  select 7, '建立超過 3 天、從沒送達過的裝置', never_ok::text, '', 'INFO' from totals
  union all
  select 8, '超過 30 天沒打開網站的裝置', stale::text, '', 'INFO' from totals
  union all
  select 9, '每人最多幾台裝置', max_devices::text, '≤ 5',
         case when max_devices <= 5 then 'PASS' else 'FAIL' end
    from per_user
  union all
  select 10, '不在白名單的推播網址', not_allowed::text, '0',
         case when not_allowed = 0 then 'PASS' else 'FAIL' end
    from totals
)
select metric, value, target, result from rows order by ord;
