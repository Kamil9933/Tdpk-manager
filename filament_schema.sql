-- Filament raw-material inventory: log 1kg spools as you buy them, consume
-- grams automatically as orders get fulfilled (matched to a product's color
-- variant), and get a reorder suggestion before you run out. Ledger-style
-- (purchases + consumption rows), same convention as the rest of the
-- accounting schema, rather than a single mutable "current stock" number --
-- current stock is always SUM(purchases) - SUM(consumption) for a type, so
-- it's fully auditable and nothing can silently drift. Run this once in the
-- Supabase SQL editor.

create table if not exists filament_types (
    id uuid primary key default gen_random_uuid(),
    material text not null default 'PLA',
    color text not null,
    reorder_threshold_grams numeric not null default 200,
    spool_weight_grams numeric not null default 1000,
    vendor_id uuid references vendors(id) on delete set null,
    notes text default '',
    active boolean not null default true,
    created_at timestamptz default now(),
    updated_at timestamptz default now(),
    unique(material, color)
);

create table if not exists filament_purchases (
    id uuid primary key default gen_random_uuid(),
    filament_type_id uuid not null references filament_types(id) on delete cascade,
    grams numeric not null,
    cost numeric default 0,
    vendor_id uuid references vendors(id) on delete set null,
    purchase_date date not null default current_date,
    notes text default '',
    created_at timestamptz default now()
);

-- One row per order line item's filament draw. filament_type_id is NULL
-- when auto-matching couldn't confidently resolve it (multi-colour product,
-- no matching spool colour, etc.) -- those show up in the "needs review"
-- list for a one-time manual fix, which can optionally remember the
-- resolution in product_filament_links so it auto-matches next time.
create table if not exists filament_consumption (
    id uuid primary key default gen_random_uuid(),
    filament_type_id uuid references filament_types(id) on delete set null,
    grams numeric not null default 0,
    shopify_order_id bigint,
    shopify_order_name text default '',
    product_id bigint,
    variant_id bigint,
    product_title text default '',
    variant_title text default '',
    matched text not null default 'auto' check (matched in ('auto','manual','needs_review')),
    created_at timestamptz default now()
);

-- A remembered (product, variant) -> filament type + grams-per-unit mapping.
-- variant_id NULL means "applies to every variant of this product unless a
-- more specific row with a variant_id exists". Populated either by resolving
-- a needs-review item ("remember this") or by direct manual entry.
create table if not exists product_filament_links (
    id uuid primary key default gen_random_uuid(),
    product_id bigint not null,
    variant_id bigint,
    filament_type_id uuid not null references filament_types(id) on delete cascade,
    grams_per_unit numeric not null,
    created_at timestamptz default now(),
    updated_at timestamptz default now(),
    unique(product_id, variant_id)
);

create index if not exists idx_filament_consumption_type  on filament_consumption(filament_type_id, created_at);
create index if not exists idx_filament_consumption_order on filament_consumption(shopify_order_id);
create index if not exists idx_product_filament_links_pv  on product_filament_links(product_id, variant_id);

alter table filament_types          enable row level security;
alter table filament_purchases      enable row level security;
alter table filament_consumption    enable row level security;
alter table product_filament_links  enable row level security;
