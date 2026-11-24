-- Photos that left a listing, waiting for their object in S3 to be deleted.
--
-- kleinanzeigen hands out new image URLs for the same ad on every scrape, and
-- the scraper used to only ever insert: one ad collected 6 820 rows for 20
-- photos, and ~1.8 M stale objects filled a quarter of the bucket's object
-- quota. The scraper now drops the rows a scrape no longer shows, but it has no
-- S3 credentials (it runs on public GitHub runners), so it leaves the image_id
-- here and image-downloader deletes `{image_id}.webp` and then the row.
--
-- No foreign key on purpose: the images row is already gone when this one is
-- written.
CREATE TABLE IF NOT EXISTS fixnflip_v2.image_deletion (
    image_id   uuid        PRIMARY KEY,
    queued_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE fixnflip_v2.image_deletion IS
 'S3 objects ({image_id}.webp) whose images row the scraper removed because the listing no longer shows that photo. image-downloader deletes the object, then the row.';
