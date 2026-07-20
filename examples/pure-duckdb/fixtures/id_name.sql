-- fixture: id_name
-- keys: [id]
-- Two columns of different types — exercises type resolution (INTEGER + VARCHAR).
CREATE TABLE id_name (id INTEGER, name VARCHAR);
INSERT INTO id_name VALUES (1, 'alice'), (2, 'bob'), (3, 'carol');
