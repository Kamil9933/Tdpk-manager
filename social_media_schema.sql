-- Social media storage (Phase 4, storage half).
-- A PUBLIC bucket so Instagram/Facebook can fetch each file by its direct URL.
-- Anyone can read; only the service role (TDPK's backend) can write or delete:
-- there are no insert/update/delete policies, and the service role bypasses RLS.
-- Safe to re-run.

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'social-media', 'social-media', true, 52428800,
  array['image/jpeg', 'image/png', 'video/mp4', 'video/quicktime']
)
on conflict (id) do update set
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists "social-media public read" on storage.objects;
create policy "social-media public read" on storage.objects
  for select to public
  using (bucket_id = 'social-media');
