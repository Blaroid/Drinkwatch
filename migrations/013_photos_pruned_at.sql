-- photos.pruned_at: set when scripts/photos-sync.sh prunes the file behind a photo_url,
-- so a row can be marked instead of serving a 404.
alter table photos add column if not exists pruned_at timestamptz;
