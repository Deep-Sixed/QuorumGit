-- Content governance: what a push may touch, and where it may land.
--
-- Protected paths name files that need an approval whenever a push brings
-- changes to them, whoever owns the branch. Allowed ref namespaces name the
-- ref prefixes a repository accepts pushes to at all; everything else is
-- refused. Every repository, existing or new, starts with branches only
-- (refs/heads/); tags, notes, and custom refs must be opted into explicitly.

CREATE TABLE protected_paths (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    path_glob TEXT NOT NULL CHECK (path_glob <> ''),
    UNIQUE (repository_id, path_glob)
);

-- quorumgit-statement
CREATE TABLE allowed_ref_namespaces (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    prefix TEXT NOT NULL
        CHECK (prefix LIKE 'refs/%/' AND length(prefix) > length('refs//')),
    UNIQUE (repository_id, prefix)
);

-- quorumgit-statement
INSERT INTO allowed_ref_namespaces (repository_id, prefix)
SELECT id, 'refs/heads/' FROM repositories;

-- quorumgit-statement
CREATE TRIGGER repositories_default_ref_namespaces
AFTER INSERT ON repositories
BEGIN
    INSERT INTO allowed_ref_namespaces (repository_id, prefix)
    VALUES (NEW.id, 'refs/heads/');
END;
