-- ===========================================================================
-- Storage bucket for generated imagery
--
-- Run after 20260716000001_init.sql.
--
-- IF YOU CHANGE THE BUCKET NAME, change it in three places:
--   1. this file
--   2. SUPABASE_BUCKET in .env
--   3. nothing else — the backend reads it from env and paths are relative.
--
-- Folder layout inside the bucket:
--
--   products/plates/<slug>.png                  auto-cropped product references
--   runs/<run_key>/<place_id>/before.jpg        the real frontage as captured
--   runs/<run_key>/<place_id>/after_a<n>.png    composite, per attempt
--   runs/<run_key>/<place_id>/prompt_a<n>.txt   the exact prompt used
--
-- Keyed by run first, then venue: a run is the unit you inspect, compare or
-- delete, and this makes "everything from run X" a single prefix. Keeping every
-- attempt (not just the winner) means a rejected generation and the prompt that
-- produced it stay side by side — which is the point of the rejection log.
-- ===========================================================================

-- Public bucket: the demo must render for a reviewer with no session and no
-- keys, and signed URLs would expire between the run and the review. The content
-- is public storefronts and generated marketing visuals — nothing to protect.
-- Write access is still service_role only (policies below).
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'storefront-visuals',
    'storefront-visuals',
    true,
    10485760,  -- 10 MB: a 640px JPEG is ~100 KB and a composite ~1-2 MB. Well clear, but bounded.
    array['image/jpeg', 'image/png', 'image/webp', 'text/plain']
)
on conflict (id) do update
set public             = excluded.public,
    file_size_limit    = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

-- ---------------------------------------------------------------------------
-- Policies
--
-- Anyone may read. Only service_role may write — and service_role bypasses RLS,
-- so it needs no policy of its own. The absence of insert/update/delete policies
-- is what stops the anon key (which ships to the browser) writing to the bucket.
-- ---------------------------------------------------------------------------

drop policy if exists "storefront visuals are publicly readable" on storage.objects;
create policy "storefront visuals are publicly readable"
    on storage.objects for select
    to anon, authenticated
    using (bucket_id = 'storefront-visuals');
