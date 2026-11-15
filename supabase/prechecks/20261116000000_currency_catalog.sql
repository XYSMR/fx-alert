-- 20261116000000_currency_catalog 的事前檢查：列出不符合「報價單位 0.0001」新限制的規則。
-- 部署 workflow 在唯讀交易裡執行；回傳任何一列就停止、不套用。
-- 修正方式：在 SQL Editor 執行這段找出規則，把目標價、回升門檻改成 4 位小數以內（回升門檻至少 0.0001）。
select id
from public.alert_rules
where target <> round(target, 4)
   or (buffer is not null
       and (buffer < 0.0001
            or buffer <> round(buffer, 4)
            or buffer > greatest(target * 0.1, 0.0001)))
