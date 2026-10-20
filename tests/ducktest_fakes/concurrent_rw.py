"""Concurrent writers appending to one DuckDB database file, each with a warm shell of its own.

DuckDB hands a database file to one process at a time, so a writer that arrives while another one
holds it is told so at ATTACH and comes back later. Every append that does get in reads `max(id)`
while it holds the file, so after K appends the ids are exactly 1..K, with no gap and no repeat.

That is the shape of a catalog commit that can conflict: try it, read the answer, retry on the one
error that means "someone was first", fail on any other.
"""

import concurrent.futures

from ducktest import test

WRITERS = 3
APPENDS = 3


@test()
def concurrent_writers_append_without_a_gap(ctx):
    shared = ctx.location("shared.db")
    maker = ctx.client("duck")
    maker.setup(
        """
        ATTACH '{db}' AS shared;
        CREATE TABLE shared.ids (id INTEGER, who VARCHAR);
        DETACH shared
        """,
        db=shared,
    )

    def append(db, who):
        """One attempt. True: the row is in. False: another process holds the file, come back."""
        attached = db.exec("ATTACH '{db}' AS shared", db=shared)
        if not attached:
            if "lock" not in attached.error.lower():
                raise AssertionError(f"ATTACH failed for a reason this test does not expect\n{attached}")
            return False
        try:
            db.oks("INSERT INTO shared.ids SELECT coalesce(max(id), 0) + 1, '{who}' FROM shared.ids", who=who)
        finally:
            db.oks("DETACH shared")
        return True

    def writer(db, who):
        committed = 0
        while committed < APPENDS:
            committed += append(db, who)
        return committed

    writers = [(ctx.client("duck", new=True), f"w{n}") for n in range(WRITERS)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=WRITERS) as pool:
        submitted = [pool.submit(writer, db, who) for db, who in writers]
        assert [f.result() for f in submitted] == [APPENDS] * WRITERS

    maker.oks("ATTACH '{db}' AS shared", db=shared)
    ids = [row[0] for row in maker.rows("SELECT id FROM shared.ids ORDER BY id")]
    assert ids == list(range(1, len(ids) + 1))
    maker.expects("SELECT count(*) FROM shared.ids", [WRITERS * APPENDS])
