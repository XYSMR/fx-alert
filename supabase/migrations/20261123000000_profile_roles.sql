-- 使用者角色（RBAC）：user（預設）、beta（搶先試用）、admin。
-- feature_flags.allowed_roles 裡的角色不看 rollout，一律開啟（20261123000100_feature_flags）。
--
-- 使用者不能改自己的角色：profiles 只對 authenticated 開放 update (email_enabled)
-- （20261109000000 的欄位權限），新欄位預設沒有任何人能透過 API 修改。
-- 改角色只能用 SQL Editor 或 secret key（service_role）：
--   update public.profiles set role = 'beta' where email = '...';

alter table public.profiles
  add column role text not null default 'user'
    check (role in ('user', 'beta', 'admin'));
