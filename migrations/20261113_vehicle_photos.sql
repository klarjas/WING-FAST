-- WING FAST: attach Telegram-stored vehicle media to the corresponding vehicle row.
-- Run once in Supabase SQL Editor before deploying the matching backend/frontend.

ALTER TABLE public.vehicles
    ADD COLUMN IF NOT EXISTS photo_media_id UUID NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'vehicles_photo_media_id_fkey'
          AND conrelid = 'public.vehicles'::regclass
    ) THEN
        ALTER TABLE public.vehicles
            ADD CONSTRAINT vehicles_photo_media_id_fkey
            FOREIGN KEY (photo_media_id)
            REFERENCES public.media_files(id)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS vehicles_photo_media_id_idx
    ON public.vehicles(photo_media_id);
