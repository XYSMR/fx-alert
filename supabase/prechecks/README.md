# 事前檢查（precheck）

migration 加了新限制（check constraint 等）時，在這裡放一個同版本號的檢查：

    supabase/prechecks/<版本號>_<名稱>.sql

內容是一段 **select**，列出「不符合新限制」的資料。部署 workflow 套用前會在**唯讀交易**裡執行：

- 回傳 0 列 → 繼續套用
- 回傳任何一列 → 停止，什麼都不寫；log 只印列數，不印內容

列出來的資料請自己在 SQL Editor 執行同一段 SQL 查看、修正後再重跑 workflow。
