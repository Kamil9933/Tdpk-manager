-- TD Shopify Manager — Accounting module schema (additive migration)
-- Run this once in your Supabase project's SQL Editor (Database > SQL Editor > New query),
-- AFTER you've already run supabase_schema.sql. This only adds new tables for the
-- Accounts tab -- it does not touch anything you already have.

create extension if not exists pgcrypto;

-- ── Vendors (filament/printer suppliers, print farms you outsource to, service providers) ──
create table if not exists vendors (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    type text not null default 'other', -- filament_supplier | printer_supplier | print_farm | service | other
    contact text,
    notes text,
    created_at timestamptz not null default now()
);

-- Purchases from a vendor (filament, printer, packaging, parts, etc.)
create table if not exists vendor_purchases (
    id uuid primary key default gen_random_uuid(),
    vendor_id uuid references vendors(id) on delete set null,
    category text not null default 'other', -- filament | printer | packaging | parts | other
    description text,
    quantity numeric,
    unit text,
    unit_cost numeric,
    total_cost numeric not null default 0,
    purchase_date date not null default current_date,
    payment_status text not null default 'paid', -- paid | partial | credit
    amount_paid numeric not null default 0,
    amount_due numeric not null default 0,
    receipt_note text, -- free-text reference/description of receipt (photo not stored, just noted)
    created_at timestamptz not null default now()
);

-- Printers as tracked assets
create table if not exists printers (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    model text,
    vendor_id uuid references vendors(id) on delete set null,
    purchase_cost numeric not null default 0,
    purchase_date date,
    useful_life_years numeric not null default 3,
    salvage_value numeric not null default 0,
    status text not null default 'active', -- active | retired
    total_hours numeric not null default 0, -- running total, kept in sync from printer_usage_logs
    created_at timestamptz not null default now()
);

-- Running-hours log per printer (used to accumulate printers.total_hours)
create table if not exists printer_usage_logs (
    id uuid primary key default gen_random_uuid(),
    printer_id uuid references printers(id) on delete cascade,
    log_date date not null default current_date,
    hours numeric not null,
    notes text,
    created_at timestamptz not null default now()
);

-- Maintenance/repair history per printer
create table if not exists printer_maintenance_logs (
    id uuid primary key default gen_random_uuid(),
    printer_id uuid references printers(id) on delete cascade,
    log_date date not null default current_date,
    type text not null default 'routine', -- routine | repair | part_replacement
    description text,
    cost numeric not null default 0,
    created_at timestamptz not null default now()
);

-- Manually-entered custom/made-to-order orders that don't go through Shopify
create table if not exists custom_orders (
    id uuid primary key default gen_random_uuid(),
    customer_name text not null,
    contact text,
    description text,
    sale_price numeric not null default 0,
    tax_amount numeric not null default 0,
    cost_estimate numeric not null default 0,
    payment_status text not null default 'unpaid', -- unpaid | deposit | paid
    amount_paid numeric not null default 0,
    production_type text not null default 'in_house', -- in_house | outsourced
    status text not null default 'quoted', -- quoted | confirmed | printing | shipped | completed | cancelled
    order_date date not null default current_date,
    delivery_date date,
    notes text,
    created_at timestamptz not null default now()
);

-- Jobs sent to an outsourced print farm (for big orders you don't print in-house)
create table if not exists outsourced_jobs (
    id uuid primary key default gen_random_uuid(),
    vendor_id uuid references vendors(id) on delete set null, -- the print farm
    order_ref text, -- free-text reference: Shopify order #, custom order id, etc.
    description text,
    quantity integer not null default 1,
    cost_charged numeric not null default 0,
    date_sent date not null default current_date,
    expected_return_date date,
    actual_return_date date,
    status text not null default 'sent', -- sent | in_progress | received | issue
    notes text,
    created_at timestamptz not null default now()
);

-- General operating expenses (ads, rent, utilities, subscriptions, courier fees, etc.)
create table if not exists expenses (
    id uuid primary key default gen_random_uuid(),
    category text not null default 'other', -- ads | rent | utilities | subscriptions | labor | shipping_supplies | courier_fees | other
    description text,
    amount numeric not null default 0,
    expense_date date not null default current_date,
    recurring boolean not null default false,
    recurrence_period text, -- monthly | weekly | null
    vendor_id uuid references vendors(id) on delete set null,
    created_at timestamptz not null default now()
);

-- Employees / contractors you pay
create table if not exists employees (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    role text,
    payment_type text not null default 'fixed_salary', -- fixed_salary | hourly | per_job
    rate numeric not null default 0,
    contact text,
    active boolean not null default true,
    start_date date,
    created_at timestamptz not null default now()
);

-- Payments made to employees/contractors
create table if not exists employee_payments (
    id uuid primary key default gen_random_uuid(),
    employee_id uuid references employees(id) on delete cascade,
    amount numeric not null default 0,
    payment_date date not null default current_date,
    period_covered text, -- e.g. "Aug 2026"
    notes text,
    created_at timestamptz not null default now()
);

-- Cash/payment accounts (bank, JazzCash/Easypaisa, cash in hand, PostEx pending payout)
create table if not exists cash_accounts (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    account_type text not null default 'bank', -- bank | mobile_wallet | cash | courier_pending
    opening_balance numeric not null default 0,
    created_at timestamptz not null default now()
);

-- Money movements in/out of a cash account
create table if not exists cash_transactions (
    id uuid primary key default gen_random_uuid(),
    account_id uuid references cash_accounts(id) on delete cascade,
    direction text not null, -- in | out
    amount numeric not null default 0,
    category text, -- sale | expense | vendor_purchase | payout | transfer | employee_payment | other
    reference text, -- free-text link to a related record
    txn_date date not null default current_date,
    notes text,
    created_at timestamptz not null default now()
);

-- Logged PostEx settlement batches (they pay out periodically, minus their fee) --
-- used to reconcile "orders delivered" against "cash actually received"
create table if not exists postex_payouts (
    id uuid primary key default gen_random_uuid(),
    payout_date date not null default current_date,
    gross_amount numeric not null default 0,
    fee_deducted numeric not null default 0,
    net_amount numeric not null default 0,
    order_count integer not null default 0,
    notes text,
    created_at timestamptz not null default now()
);

-- Same rationale as supabase_schema.sql: the app only ever connects with the service_role
-- key (bypasses RLS), so this just closes off the anon/public key as a safety net.
alter table vendors                  enable row level security;
alter table vendor_purchases         enable row level security;
alter table printers                 enable row level security;
alter table printer_usage_logs       enable row level security;
alter table printer_maintenance_logs enable row level security;
alter table custom_orders            enable row level security;
alter table outsourced_jobs          enable row level security;
alter table expenses                 enable row level security;
alter table employees                enable row level security;
alter table employee_payments        enable row level security;
alter table cash_accounts            enable row level security;
alter table cash_transactions        enable row level security;
alter table postex_payouts           enable row level security;
