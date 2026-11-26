-- A10 實驗的 HDD 指標報表（A11，issue #59）。
-- 在 Supabase 的 SQL Editor 整段執行（postgres 身分），只讀，不改任何資料。
-- 不是 migration：部署 workflow 不會執行它。
--
-- 實驗期間從 hdd.since 起算，沒設定就用 2026-12-06（quick_target 開始 20% rollout）。
-- 換 rollout 階段時重設起點，在同一個查詢分頁先執行：
--   select set_config('hdd.since', '2026-12-10', false);
-- 原因：誰在 ON 組是用「目前的」flag 設定推算的，rollout 比例中途改了，舊期間就算不準。
--
-- 計算原則
--   * 只數不重複的使用者，每人每種事件只看第一筆：
--     experiment_events 允許同一人重複送事件，直接 count(*) 會被灌水（PR #54 review）。
--   * 分組（ON / OFF）：和 public.is_feature_on() 相同的規則推算，
--       enabled 而且（角色在 allowed_roles，或 md5 分桶 < rollout）。
--     experiment_events 只記錄有行動的人；沒行動的人要靠推算才有分母。
--   * H2 只看實驗期間內才註冊的新使用者（profiles.created_at = 第一次登入）。
--   * result：PASS / FAIL 對照 A10 的假設門檻；INFO 是參考值；NO DATA 是樣本不夠。
--
-- 門檻（docs/rollout-plan.md）
--   H1 trip_plan     ON 組 ≥ 30% 的使用者建立旅程（樣本至少 10 人）
--   H2 quick_target  ON 組新使用者「第一次登入 → 第一個提醒」中位數 < 60 秒（樣本至少 5 人）
--   Guardrail H2     ON 組 24 小時內修改提醒的比例不高於 OFF 組
--   Guardrail H1     最近一次匯率檢查成功、沒有寄信錯誤（錯誤率歷史和執行時間看 GitHub Actions）

with params as (
  select coalesce(nullif(current_setting('hdd.since', true), ''), '2026-12-06')::timestamptz as since
),

-- 每個使用者在每個 flag 被分到哪一組（推算，規則同 is_feature_on）
assignment as (
  select p.user_id,
         p.created_at as signed_up_at,
         f.flag,
         case
           when f.enabled
                and (p.role = any (f.allowed_roles)
                     or ('x' || substr(md5(f.flag || ':' || p.user_id::text), 1, 8))::bit(32)::bigint % 100
                        < f.rollout)
             then 'on'
           else 'off'
         end as variant
    from public.profiles p
    cross join public.feature_flags f
),

-- 每人每種事件的第一筆
first_event as (
  select e.user_id, e.flag, e.event, min(e.created_at) as first_at
    from public.experiment_events e
    cross join params
   where e.created_at >= params.since
   group by e.user_id, e.flag, e.event
),

h1 as (
  select a.variant,
         count(*)          as exposed,
         count(fe.user_id) as created
    from assignment a
    left join first_event fe
      on fe.user_id = a.user_id and fe.flag = 'trip_plan' and fe.event = 'trip_created'
   where a.flag = 'trip_plan'
   group by a.variant
),

h2 as (
  select a.variant,
         count(*)          as new_users,
         count(fe.user_id) as saved,
         percentile_cont(0.5) within group (
           order by extract(epoch from fe.first_at - a.signed_up_at)
         ) filter (where fe.user_id is not null) as median_s
    from assignment a
    cross join params
    left join first_event fe
      on fe.user_id = a.user_id and fe.flag = 'quick_target' and fe.event = 'alert_saved'
   where a.flag = 'quick_target' and a.signed_up_at >= params.since
   group by a.variant
),

-- 24 小時內「使用者自己」修改過的提醒。
-- updated_at 在排程改 state 時也會更新，所以排除和 state_changed_at 同時發生的更新。
-- 已刪除的提醒查不到（刪了就沒有那一列），所以這個數字只會偏低。
g2 as (
  select a.variant,
         count(r.id) as alerts,
         count(r.id) filter (
           where r.updated_at > r.created_at + interval '1 second'
             and r.updated_at < r.created_at + interval '24 hours'
             and (r.state_changed_at is null
                  or r.updated_at > r.state_changed_at + interval '1 second')
         ) as edited
    from assignment a
    cross join params
    join public.alert_rules r on r.user_id = a.user_id and r.created_at >= params.since
   where a.flag = 'quick_target'
   group by a.variant
),

g2_share as (
  select max(edited::numeric / nullif(alerts, 0)) filter (where variant = 'on')  as on_share,
         max(edited::numeric / nullif(alerts, 0)) filter (where variant = 'off') as off_share,
         coalesce(sum(alerts) filter (where variant = 'on'), 0)  as on_alerts,
         coalesce(sum(alerts) filter (where variant = 'off'), 0) as off_alerts
    from g2
),

report (seq, metric, flag, variant, value, target, result) as (
  -- H1：ON 組建立旅程的比例
  select 1, 'H1 users who created a trip', 'trip_plan', v.variant,
         coalesce(h1.created, 0) || ' / ' || coalesce(h1.exposed, 0)
           || coalesce(' = ' || round(100.0 * h1.created / nullif(h1.exposed, 0), 1) || '%', ''),
         case when v.variant = 'on' then '>= 30%' else 'baseline' end,
         case
           when v.variant = 'off' then 'INFO'
           when coalesce(h1.exposed, 0) < 10 then 'NO DATA'
           when h1.created::numeric / h1.exposed >= 0.30 then 'PASS'
           else 'FAIL'
         end
    from (values ('on'), ('off')) v (variant)
    left join h1 on h1.variant = v.variant

  union all
  -- H2：新使用者第一次登入到第一個提醒的中位數
  select 2, 'H2 median login -> first alert', 'quick_target', v.variant,
         coalesce(round(h2.median_s::numeric, 1) || ' s', '-')
           || ' (' || coalesce(h2.saved, 0) || ' of ' || coalesce(h2.new_users, 0) || ' new users)',
         case when v.variant = 'on' then '< 60 s' else 'baseline' end,
         case
           when v.variant = 'off' then 'INFO'
           when coalesce(h2.saved, 0) < 5 then 'NO DATA'
           when h2.median_s < 60 then 'PASS'
           else 'FAIL'
         end
    from (values ('on'), ('off')) v (variant)
    left join h2 on h2.variant = v.variant

  union all
  -- Guardrail H2：ON 組修改提醒的比例不能比 OFF 組高
  select 3, 'Guardrail: alerts edited within 24 h', 'quick_target', 'on vs off',
         coalesce(round(100 * on_share, 1) || '%', '-') || ' vs '
           || coalesce(round(100 * off_share, 1) || '%', '-'),
         'on <= off',
         case
           when on_alerts < 5 or off_alerts < 5 then 'NO DATA'
           when on_share <= off_share then 'PASS'
           else 'FAIL'
         end
    from g2_share

  union all
  -- Guardrail H1：最近一次匯率檢查
  select 4, 'Guardrail: latest rate check', 'trip_plan', '-',
         coalesce(case when c.ok then 'ok' else 'failed' end
                  || ', ' || c.errors || ' mail errors, ' || to_char(c.checked_at, 'YYYY-MM-DD HH24:MI'),
                  'no check yet'),
         'ok, 0 errors',
         case
           when c.id is null then 'NO DATA'
           when c.ok and c.errors = 0 then 'PASS'
           else 'FAIL'
         end
    from (select 1) one
    left join public.check_status c on true
)

select metric, flag, variant, value, target, result
  from report
 order by seq, variant desc;
