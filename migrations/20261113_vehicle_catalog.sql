-- WING FAST: Dynamic vehicle catalogue.
-- Run once in Supabase SQL Editor after media and earlier vehicle-image migrations.

CREATE TABLE IF NOT EXISTS public.vehicle_catalog (
    vehicle_type TEXT PRIMARY KEY CHECK (vehicle_type ~ '^[a-z][a-z0-9_]{1,39}$'),
    display_name TEXT NOT NULL CHECK (char_length(display_name) BETWEEN 2 AND 60),
    price_per_km NUMERIC(10,2) NOT NULL DEFAULT 0 CHECK (price_per_km >= 0),
    base_fare NUMERIC(10,2) NOT NULL DEFAULT 0 CHECK (base_fare >= 0),
    minimum_fare NUMERIC(10,2) NOT NULL DEFAULT 0 CHECK (minimum_fare >= 0),
    media_id UUID NULL REFERENCES public.media_files(id) ON DELETE SET NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order INTEGER NOT NULL DEFAULT 100,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Import current five types, inheriting their current pricing and media if configured.
INSERT INTO public.vehicle_catalog
    (vehicle_type, display_name, price_per_km, base_fare, minimum_fare, media_id, is_active, sort_order)
SELECT seed.vehicle_type, seed.display_name,
       COALESCE(pr.price_per_km,0), COALESCE(pr.base_fare,0),
       COALESCE(pr.minimum_fare,0), vi.media_id, TRUE, seed.sort_order
FROM (VALUES
    ('suv','SUV',10),('car','Sedan',20),('tuk_tuk','Tuk-Tuk',30),
    ('motorbike','Motorcycle',40),('van','Van',50)
) AS seed(vehicle_type,display_name,sort_order)
LEFT JOIN public.pricing pr ON pr.vehicle_type=seed.vehicle_type AND pr.is_active=TRUE
LEFT JOIN public.vehicle_type_images vi ON vi.vehicle_type=seed.vehicle_type
ON CONFLICT (vehicle_type) DO NOTHING;

-- Remove old fixed-value vehicle type constraints so Admin-created types can be used.
DO $$
DECLARE item RECORD;
BEGIN
    FOR item IN
        SELECT conrelid::regclass AS table_name, conname
        FROM pg_constraint
        WHERE contype='c'
          AND conrelid IN ('public.vehicles'::regclass,'public.bookings'::regclass)
          AND pg_get_constraintdef(oid) ILIKE '%vehicle_type%'
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',item.table_name,item.conname);
    END LOOP;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='vehicles_vehicle_type_slug_check' AND conrelid='public.vehicles'::regclass) THEN
        ALTER TABLE public.vehicles ADD CONSTRAINT vehicles_vehicle_type_slug_check
        CHECK (vehicle_type ~ '^[a-z][a-z0-9_]{1,39}$');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='bookings_vehicle_type_slug_check' AND conrelid='public.bookings'::regclass) THEN
        ALTER TABLE public.bookings ADD CONSTRAINT bookings_vehicle_type_slug_check
        CHECK (vehicle_type='all' OR vehicle_type ~ '^[a-z][a-z0-9_]{1,39}$');
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS vehicle_catalog_active_sort_idx
ON public.vehicle_catalog(is_active,sort_order);
