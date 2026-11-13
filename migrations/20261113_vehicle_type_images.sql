-- WING FAST: admin-managed default vehicle category images.
-- Run once in Supabase SQL Editor.

CREATE TABLE IF NOT EXISTS public.vehicle_type_images (
    vehicle_type TEXT PRIMARY KEY
        CHECK (vehicle_type IN ('suv','car','tuk_tuk','motorbike','van')),
    media_id UUID NOT NULL
        REFERENCES public.media_files(id)
        ON DELETE RESTRICT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS vehicle_type_images_media_id_idx
    ON public.vehicle_type_images(media_id);
