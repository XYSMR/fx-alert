-- 角色（RBAC）：每個使用者一個角色，之後的 feature toggle 用來判斷「誰可以用」。
--
--   user   一般使用者（預設）：依 rollout 百分比決定看不看得到新功能
--   beta   測試者：新功能一律開啟
--   admin  管理者：新功能一律開啟
--
-- * 只有 service_role（SQL Editor、排程）能改角色。
--   authenticated 只有 update (email_enabled, notify_start, notify_end) 的「欄位權限」，
--   新欄位 role 不在清單裡 → 使用者改不了自己的角色（不需要另外寫 trigger）。
-- * 使用者讀得到自己的角色（profiles 的 select 權限＋「read own profile」RLS）。
-- * 權限一律在伺服器端（RLS / 函式）判斷；前端隱藏按鈕只是介面，不是權限控管。

alter table public.profiles
  add column if not exists role text not null default 'user';

alter table public.profiles drop constraint if exists profiles_role_valid;
alter table public.profiles add constraint profiles_role_valid
  check (role in ('user', 'beta', 'admin'));

comment on column public.profiles.role is
  '角色：user（預設）/ beta / admin；只有 service_role 能改';

-- 目前登入者的角色；沒登入或沒有 profile → null。
-- 給 RLS policy 和 feature flag 判斷用（security invoker：照呼叫者自己的權限讀，RLS 照樣生效）。
create or replace function public.current_user_role()
returns text
language sql
stable
security invoker
set search_path = ''
as $$
  select p.role from public.profiles p where p.user_id = (select auth.uid());
$$;

revoke all on function public.current_user_role() from public, anon;
grant execute on function public.current_user_role() to authenticated, service_role;
