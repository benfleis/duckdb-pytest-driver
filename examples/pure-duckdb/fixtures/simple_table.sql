-- fixture: simple_table
-- keys: [id]
-- One integer column, five rows. The canonical "does it round-trip" fixture.
CREATE TABLE simple_table (id INTEGER);
INSERT INTO simple_table VALUES (1), (2), (3), (4), (5);
