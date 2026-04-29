-- Supabase 회원 DB 스키마
-- 운영: Supabase 콘솔에서 SQL Editor → 본 파일 붙여넣기

create table if not exists members (
  member_id        text primary key,
  name             text not null,
  phone            text not null,
  email            text not null,
  plan             text not null check (plan in ('lite','standard','pro','annual')),
  markets          text[] not null,
  agree_terms      boolean not null,
  agree_privacy    boolean not null,
  agree_alimtalk   boolean not null,
  agree_marketing  boolean default false,
  billing_key      text,
  is_active        boolean default true,
  trial_ends_at    timestamptz not null,
  created_at       timestamptz default now()
);

create index if not exists idx_members_active on members (is_active);
create index if not exists idx_members_phone  on members (phone);

-- 발송 이력 (3년 보관 — 분쟁·민원 대응)
create table if not exists send_logs (
  id          bigserial primary key,
  member_id   text references members (member_id),
  market      text not null check (market in ('kr','us','futures')),
  template_id text not null,
  content     text not null,
  status      text not null check (status in ('queued','sent','failed','fallback_sms')),
  vendor_msg_id text,
  error       text,
  sent_at     timestamptz default now()
);

create index if not exists idx_send_logs_member on send_logs (member_id, sent_at desc);

-- 약관 동의 로그 (개정 시 재동의 추적)
create table if not exists consent_logs (
  id          bigserial primary key,
  member_id   text references members (member_id),
  consent_type text not null,
  agreed      boolean not null,
  ip          text,
  user_agent  text,
  agreed_at   timestamptz default now()
);
