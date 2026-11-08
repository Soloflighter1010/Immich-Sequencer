import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import database


class DatabaseMigrationTests(unittest.TestCase):
    def test_init_db_adds_asset_count_without_replacing_existing_album(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "sequencer.db")

            # Simulate a database from an older release. It already contains
            # album state, but predates the asset_count column.
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE albums (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'unsorted'
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO albums (id, name, status) VALUES (?, ?, ?)",
                    ("existing-album", "Existing Album", "sorted"),
                )

            with patch.object(database, "DB_PATH", db_path):
                database.init_db()
                with database.get_db() as conn:
                    columns = {
                        row["name"]
                        for row in conn.execute("PRAGMA table_info(albums)")
                    }
                    row = conn.execute(
                        "SELECT id, name, status, asset_count FROM albums WHERE id = ?",
                        ("existing-album",),
                    ).fetchone()

                self.assertIn("asset_count", columns)
                self.assertEqual(row["name"], "Existing Album")
                self.assertEqual(row["status"], "sorted")
                self.assertEqual(row["asset_count"], 0)

                # The normal scan upsert must work after migration.
                status = database.upsert_album_from_scan(
                    "existing-album", "Existing Album", 12
                )
                self.assertEqual(status, "sorted")
                with database.get_db() as conn:
                    updated = conn.execute(
                        "SELECT asset_count FROM albums WHERE id = ?",
                        ("existing-album",),
                    ).fetchone()
                self.assertEqual(updated["asset_count"], 12)

                # Startup migration is repeatable.
                database.init_db()


if __name__ == "__main__":
    unittest.main()
