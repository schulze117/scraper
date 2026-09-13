import json
import time
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import psycopg
from psycopg.sql import SQL, Placeholder, Composed, Identifier

from lib.config import get_config, get_env
from lib.models import ListingSource, NewListing, NextListingModel
from datetime import datetime
import zoneinfo
from lib.logger import get_logger

berlin_tz = zoneinfo.ZoneInfo("Europe/Berlin")

config = get_config()
env = get_env()


GET_KLEINANZEIGEN_IDS_BY_STATE_SQL = SQL(
    """
    SELECT DISTINCT f.kleinanzeigen_location_id
    FROM germany.custom f
    JOIN germany.zuordnung_plz_ags z ON f.plz = z.plz
    WHERE z.bundesland = {state}
    """
).format(state=Placeholder("state"))

SET_NEW_LISTING_DATA_SQL: Composed = SQL(
    "INSERT INTO {schema}.{table} ({fields}) VALUES ({values}) " "ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
).format(
    schema=Identifier("fixnflip_v2"),
    table=Identifier("property"),
    fields=SQL(", ").join(
        [
            Identifier("source"),
            Identifier("external_id"),
            Identifier("modified_at"),
            Identifier("created_at"),
        ]
    ),
    values=SQL(", ").join(
        [
            Placeholder("source"),
            Placeholder("external_id"),
            Placeholder("modified_at"),
            Placeholder("created_at"),
        ]
    ),
    conflict=SQL(", ").join([Identifier("external_id"), Identifier("source")]),
    updates=SQL("{0} = EXCLUDED.{0}").format(Identifier("modified_at")),
)

SELECT_PROPERTY_IDS_SQL = SQL(
    """
    SELECT id, external_id
    FROM {schema}.{table}
    WHERE (external_id, source::text) IN (SELECT unnest(%(external_ids)s::text[]), unnest(%(sources)s::text[]))
    """
).format(schema=Identifier("fixnflip_v2"), table=Identifier("property"))

GENERAL_INSERT_SQL: Composed = SQL(
    # Dont update active status to True on conflicts since this will be handled by scraper and not finder
    "INSERT INTO {schema}.{table} ({fields}) VALUES ({values}) ON CONFLICT ({conflict}) DO NOTHING"
).format(
    schema=Identifier("fixnflip_v2"),
    table=Identifier("general"),
    fields=SQL(", ").join([Identifier("property_id"), Identifier("active")]),
    values=SQL(", ").join([Placeholder("property_id"), Placeholder("active")]),
    conflict=Identifier("property_id"),
)

SYSTEM_INSERT_SQL: Composed = SQL(
    "INSERT INTO {schema}.{table} ({fields}) VALUES ({values}) " "ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
).format(
    schema=Identifier("fixnflip_v2"),
    table=Identifier("system"),
    fields=SQL(", ").join([Identifier("property_id"), Identifier("last_seen_at")]),
    values=SQL(", ").join([Placeholder("property_id"), Placeholder("last_seen_at")]),
    conflict=Identifier("property_id"),
    updates=SQL("{0} = EXCLUDED.{0}").format(Identifier("last_seen_at")),
)

CLAIM_EXPIRY_INTERVAL = "5 minutes"

GET_NEXT_LISTINGS_SQL: Composed = SQL(
    f"""
    WITH candidates AS (
        SELECT s.property_id AS system_property_id,
            p.id,
            p.source,
            p.external_id,
            p.created_at,
            p.modified_at,
            s.last_scraped_at
        FROM fixnflip_v2.system s
        JOIN fixnflip_v2.property p ON p.id = s.property_id
        LEFT JOIN fixnflip_v2.general g ON g.property_id = p.id
        WHERE p.source = {{source}}
            AND (g.active = TRUE OR g.active IS NULL)
            AND (s.last_scraped_at IS NULL OR s.last_scraped_at < NOW() - INTERVAL '12 hours')
            AND (s.claimed_at IS NULL OR s.claimed_at < NOW() - INTERVAL '{CLAIM_EXPIRY_INTERVAL}')
        ORDER BY
            s.last_scraped_at IS NOT NULL,
            s.last_scraped_at,
            p.created_at
        LIMIT {{limit}}
        FOR UPDATE OF s SKIP LOCKED
    ),
    claim AS (
        UPDATE fixnflip_v2.system
        SET claimed_at = NOW()
        WHERE property_id IN (SELECT system_property_id FROM candidates)
    )
    SELECT id, source, external_id, created_at, modified_at, last_scraped_at
    FROM candidates;
    """
).format(source=Placeholder("source"), limit=Placeholder("limit"))

GET_NEXT_LISTINGS_MODIFIED_SQL: Composed = SQL(
    f"""
    WITH candidates AS (
        SELECT s.property_id AS system_property_id,
            p.id,
            p.source,
            p.external_id,
            p.created_at,
            p.modified_at,
            s.last_scraped_at
        FROM fixnflip_v2.system s
        JOIN fixnflip_v2.property p ON p.id = s.property_id
        LEFT JOIN fixnflip_v2.general g ON g.property_id = p.id
        WHERE p.source = {{source}}
            AND (g.active = TRUE OR g.active IS NULL)
            AND (s.last_scraped_at IS NULL OR p.modified_at > s.last_scraped_at)
            AND (s.claimed_at IS NULL OR s.claimed_at < NOW() - INTERVAL '{CLAIM_EXPIRY_INTERVAL}')
        ORDER BY
            s.last_scraped_at IS NOT NULL,
            s.last_scraped_at,
            p.created_at
        LIMIT {{limit}}
        FOR UPDATE OF s SKIP LOCKED
    ),
    claim AS (
        UPDATE fixnflip_v2.system
        SET claimed_at = NOW()
        WHERE property_id IN (SELECT system_property_id FROM candidates)
    )
    SELECT id, source, external_id, created_at, modified_at, last_scraped_at
    FROM candidates;
    """
).format(source=Placeholder("source"), limit=Placeholder("limit"))

# Overwrite on conflict, don't skip. A re-scrape only happens when the finder saw
# a newer `modified_at` than our `last_scraped_at`, i.e. the portal listing really
# changed — so the fresh HTML is the one we want. DO NOTHING kept the original
# capture forever, which made the whole re-scrape/re-extract chain a no-op:
# extract re-ran on HTML from the first sighting.
SET_RAW_DATA_SQL: Composed = SQL(
    "INSERT INTO {schema}.{table} ({fields}) VALUES ({values}) ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
).format(
    schema=Identifier("fixnflip_v2"),
    table=Identifier("raw_data"),
    fields=SQL(", ").join([Identifier("property_id"), Identifier("html"), Identifier("json")]),
    values=SQL(", ").join([Placeholder("property_id"), Placeholder("html"), Placeholder("json")]),
    conflict=Identifier("property_id"),
    updates=SQL(", ").join(
        [
            SQL("{0} = EXCLUDED.{0}").format(Identifier("html")),
            SQL("{0} = EXCLUDED.{0}").format(Identifier("json")),
            SQL("{0} = now()").format(Identifier("fetched_at")),
        ]
    ),
)

SET_IMAGE_URLS_SQL: Composed = SQL(
    "INSERT INTO {schema}.{table} ({fields}) VALUES ({values}) ON CONFLICT ({conflict}) DO NOTHING"
).format(
    schema=Identifier("fixnflip_v2"),
    table=Identifier("images"),
    fields=SQL(", ").join([Identifier("property_id"), Identifier("url")]),
    values=SQL(", ").join([Placeholder("property_id"), Placeholder("url")]),
    conflict=SQL(", ").join([Identifier("property_id"), Identifier("url")]),
)

SET_MAIN_IMAGE_URL_SQL: Composed = SQL(
    "INSERT INTO {schema}.{table} ({fields}) VALUES ({values}) ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
).format(
    schema=Identifier("fixnflip_v2"),
    table=Identifier("images"),
    fields=SQL(", ").join([Identifier("property_id"), Identifier("url"), Identifier("is_main")]),
    values=SQL(", ").join([Placeholder("property_id"), Placeholder("url"), Placeholder("is_main")]),
    conflict=SQL(", ").join([Identifier("property_id"), Identifier("url")]),
    updates=SQL("{0} = EXCLUDED.{0}").format(Identifier("is_main")),
)

SET_LAST_SCRAPED_SQL: Composed = SQL(
    "UPDATE {schema}.{table} SET {field} = now() WHERE {where_field} = {where_value}"
).format(
    schema=Identifier("fixnflip_v2"),
    table=Identifier("system"),
    field=Identifier("last_scraped_at"),
    where_field=Identifier("property_id"),
    where_value=Placeholder("property_id"),
)

DEACTIVATE_LISTING_SQL: Composed = SQL(
    "UPDATE {schema}.{table} SET {field} = FALSE WHERE {where_field} = {where_value}"
).format(
    schema=Identifier("fixnflip_v2"),
    table=Identifier("general"),
    field=Identifier("active"),
    where_field=Identifier("property_id"),
    where_value=Placeholder("property_id"),
)

DELETE_LISTING_SQL = SQL(
    """
    DELETE FROM {property_table}
    USING {system_table}
    WHERE {property_table}.id = %(property_id)s
        AND {property_table}.id = {system_table}.property_id
        AND {system_table}.last_scraped_at IS NULL
    """
).format(
    property_table=Identifier("fixnflip_v2", "property"),
    system_table=Identifier("fixnflip_v2", "system"),
)


COUNT_STALE_LISTINGS_SQL: Composed = SQL(
    """
    SELECT count(*) FILTER (WHERE s.last_seen_at < now() - %(max_age)s::interval) AS stale,
           count(*) AS active
    FROM fixnflip_v2.property p
    JOIN fixnflip_v2.system s ON s.property_id = p.id
    LEFT JOIN fixnflip_v2.general g ON g.property_id = p.id
    WHERE p.source = %(source)s
        AND (g.active = TRUE OR g.active IS NULL)
        AND s.last_seen_at IS NOT NULL
    """
)

EXPIRE_STALE_LISTINGS_SQL: Composed = SQL(
    """
    UPDATE fixnflip_v2.general g
    SET active = FALSE
    FROM fixnflip_v2.property p
    JOIN fixnflip_v2.system s ON s.property_id = p.id
    WHERE g.property_id = p.id
        AND p.source = %(source)s
        AND (g.active = TRUE OR g.active IS NULL)
        AND s.last_seen_at IS NOT NULL
        AND s.last_seen_at < now() - %(max_age)s::interval
    """
)


def safe_json_dumps(data: dict[str, Any]) -> str:
    text = json.dumps(data, ensure_ascii=False)
    return text.replace(r"\u0000", "")


def db_operation_with_retry(func):
    def wrapper(self, *args, **kwargs):
        attempts = config.database.max_retries
        delay = config.database.retry_delay
        for attempt in range(1, attempts + 1):
            try:
                return func(self, *args, **kwargs)
            except Exception as exc:
                self.logger.warning(f"DB operation failed (attempt {attempt}/{attempts}): {exc}")
                if attempt == attempts:
                    raise
                time.sleep(delay)
    return wrapper


class Database:
    def __init__(self):
        self.logger = get_logger(self.__class__.__name__)
        self._conn_kwargs = {
            "host": env.DATABASE__HOST,
            "port": int(env.DATABASE__PORT),
            "dbname": env.DATABASE__NAME,
            "user": env.DATABASE__USER,
            "password": env.DATABASE__PASSWORD,
            "connect_timeout": config.database.timeout,
        }

    @contextmanager
    def _db(self):
        conn = psycopg.connect(**self._conn_kwargs, row_factory=psycopg.rows.dict_row)
        try:
            with conn.cursor() as cursor:
                yield conn, cursor
        finally:
            conn.close()

    @db_operation_with_retry
    def get_kleinanzeigen_ids_by_state(self, state: str) -> list[str]:
        self.logger.debug(f"Getting Kleinanzeigen IDs for state: {state}")
        with self._db() as (_, cursor):
            cursor.execute(
                GET_KLEINANZEIGEN_IDS_BY_STATE_SQL,
                {"state": state},
            )
            results = cursor.fetchall()
        ids = [row["kleinanzeigen_location_id"] for row in results]
        self.logger.debug(f"Found {len(ids)} IDs for state: {state}")
        return ids

    @db_operation_with_retry
    def set_new_listing_data(self, listings: list[NewListing]) -> int:
        """Upsert listings and return how many were genuinely NEW (not already in
        the DB). The finder uses that count to stop paginating once a page has
        nothing new (results are newest-first)."""
        if not listings:
            self.logger.debug("No listings to process")
            return 0

        self.logger.debug(
            "Saving listings to DB: %s",
            [
                {
                    "external_id": l.external_id,
                    "source": l.source.value,
                    "created_at": l.created_at,
                    "modified_at": l.modified_at,
                }
                for l in listings
            ]
        )

        external_ids = [l.external_id for l in listings]
        sources = [l.source.value for l in listings]

        with self._db() as (connection, cursor):
            # Which of these already exist? (count new ones for the finder's early-stop)
            cursor.execute(SELECT_PROPERTY_IDS_SQL, {"external_ids": external_ids, "sources": sources})
            pre_existing = {row["external_id"] for row in cursor.fetchall()}
            new_count = sum(1 for eid in external_ids if eid not in pre_existing)

            self.logger.debug(f"Batch setting property data for {len(listings)} listings")
            cursor.executemany(
                SET_NEW_LISTING_DATA_SQL,
                [
                    {
                        "source": listing.source.value,
                        "external_id": listing.external_id,
                        "modified_at": listing.modified_at,
                        "created_at": listing.created_at if listing.created_at else datetime.now(berlin_tz),
                    }
                    for listing in listings
                ],
            )

            self.logger.debug(f"Selecting property IDs")

            cursor.execute(SELECT_PROPERTY_IDS_SQL, {"external_ids": external_ids, "sources": sources})
            results = cursor.fetchall()
            property_id_mapping: dict[str, str] = {row["external_id"]: row["id"] for row in results}

            if len(property_id_mapping) != len(listings):
                raise ValueError(f"Expected {len(listings)} property IDs, but got {len(property_id_mapping)}")

            self.logger.debug(f"Setting general and system data")
            cursor.executemany(
                GENERAL_INSERT_SQL,
                [
                    {
                        "property_id": property_id_mapping[listing.external_id],
                        "active": True,
                    }
                    for listing in listings
                ],
            )

            self.logger.debug(f"Setting system data")
            cursor.executemany(
                SYSTEM_INSERT_SQL,
                [
                    {
                        "property_id": property_id_mapping[listing.external_id],
                        "last_seen_at": datetime.now(berlin_tz),
                    }
                    for listing in listings
                ],
            )

            connection.commit()
            self.logger.debug(f"Batch listing data set for {len(listings)} listings ({new_count} new)")
            return new_count

    @db_operation_with_retry
    def get_next_listings(self, source: ListingSource, limit: int, rescrape_on_modified_only: bool = False) -> list[NextListingModel]:
        self.logger.debug(f"Getting next listings for source: '{source.value}' (limit: {limit}, rescrape_on_modified_only: {rescrape_on_modified_only})")
        sql = GET_NEXT_LISTINGS_MODIFIED_SQL if rescrape_on_modified_only else GET_NEXT_LISTINGS_SQL
        with self._db() as (connection, cursor):
            cursor.execute(sql, {"source": source.value, "limit": limit})
            results = cursor.fetchall()
            connection.commit()  # Commit to persist claimed_at and release row locks
        self.logger.debug(f"Found and claimed {len(results)} listings")
        return [NextListingModel(**row) for row in results]

    @db_operation_with_retry
    def set_raw_data(self, uuid: UUID, html: str, json_data: dict[str, Any]) -> None:
        with self._db() as (connection, cursor):
            self.logger.debug(f"Setting raw data for {uuid}")
            cursor.execute(
                SET_RAW_DATA_SQL,
                {"property_id": uuid, "html": html, "json": safe_json_dumps(json_data)},
            )
            connection.commit()
            self.logger.debug(f"Raw data set for {uuid}")

    @db_operation_with_retry
    def set_image_urls(self, uuid: UUID, image_urls: list[str]) -> None:
        with self._db() as (connection, cursor):
            self.logger.debug(f"Setting image URLs for {uuid}")
            cursor.executemany(
                SET_IMAGE_URLS_SQL, [{"property_id": uuid, "url": url} for url in image_urls]
            )
            connection.commit()
            self.logger.debug(f"Setting image URLs set for {uuid}")

    @db_operation_with_retry
    def set_main_image_url(self, uuid: UUID, image_url: str) -> None:
        with self._db() as (connection, cursor):
            self.logger.debug(f"Setting main image URL for {uuid}")
            cursor.execute(
                SET_MAIN_IMAGE_URL_SQL,
                {"property_id": uuid, "url": image_url, "is_main": True},
            )
            connection.commit()
            self.logger.debug(f"Main image URL set for {uuid}")

    @db_operation_with_retry
    def set_last_scraped(self, uuid: UUID) -> None:
        with self._db() as (connection, cursor):
            self.logger.debug(f"Setting last scraped for {uuid}")
            cursor.execute(SET_LAST_SCRAPED_SQL, {"property_id": uuid})
            connection.commit()
            self.logger.debug(f"Last scraped set for {uuid}")

    @db_operation_with_retry
    def deactivate_listing(self, uuid: UUID) -> None:
        with self._db() as (connection, cursor):
            self.logger.debug(f"Deactivating listing {uuid}")
            cursor.execute(DEACTIVATE_LISTING_SQL, {"property_id": uuid})
            connection.commit()
            self.logger.debug(f"Listing {uuid} deactivated")

    @db_operation_with_retry
    def delete_listing(self, uuid: UUID) -> None:
        with self._db() as (connection, cursor):
            self.logger.debug(f"Deleting listing {uuid}")
            cursor.execute(DELETE_LISTING_SQL, {"property_id": uuid})
            connection.commit()
            self.logger.debug(f"Listing {uuid} deleted")

    @db_operation_with_retry
    def count_stale_listings(self, source: ListingSource, max_age_days: int) -> tuple[int, int]:
        """(stale, active) for one source. Stale = active but not seen by the
        finder in max_age_days."""
        with self._db() as (_, cursor):
            cursor.execute(
                COUNT_STALE_LISTINGS_SQL,
                {"source": source.value, "max_age": f"{max_age_days} days"},
            )
            row = cursor.fetchone()
            return (row["stale"], row["active"]) if row else (0, 0)

    @db_operation_with_retry
    def expire_stale_listings(self, source: ListingSource, max_age_days: int) -> int:
        """Mark listings inactive that the finder has not seen in max_age_days.

        `last_seen_at` is only meaningful once the weekly sweep runs, because the
        incremental finder stops after a few pages and never reaches the older
        listings. Callers must therefore check the sweep actually completed before
        calling this — see expire.py, which refuses on an implausible share.
        """
        with self._db() as (connection, cursor):
            cursor.execute(
                EXPIRE_STALE_LISTINGS_SQL,
                {"source": source.value, "max_age": f"{max_age_days} days"},
            )
            expired = cursor.rowcount
            connection.commit()
            self.logger.info(f"Expired {expired} {source.value} listings not seen in {max_age_days} days")
            return expired

    @db_operation_with_retry
    def update_extra_data(self, uuid: UUID, extra_data: dict[str, dict[str, Any]]) -> None:
        self.logger.debug(f"Updating extra data for {uuid}")
        with self._db() as (connection, cursor):
            for table, columns in extra_data.items():
                id_column = "property_id" if table != "property" else "id"
                set_clause = SQL(", ").join(
                    SQL("{} = {}").format(Identifier(col), Placeholder(col)) for col in columns.keys()
                )
                query = SQL("UPDATE {table} SET {set_clause} WHERE {id_column} = {id_placeholder}").format(
                    table=Identifier("fixnflip_v2", table),
                    set_clause=set_clause,
                    id_column=Identifier(id_column),
                    id_placeholder=Placeholder(id_column),
                )
                values: dict[str, Any] = {**columns, id_column: uuid}
                cursor.execute(query, values)
            connection.commit()
            self.logger.debug(f"Extra data updated for {uuid}")
