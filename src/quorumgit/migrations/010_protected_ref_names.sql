-- Protected refs are matched against the full ref names Git hands the hooks
-- (refs/heads/main), so a short name stored as typed ("main") protected
-- nothing. Expand existing short names to the branch they name, dropping any
-- that duplicate a full name already protected, and refuse short names from
-- now on, including from writers that bypass the CLI.

DELETE FROM protected_refs
WHERE refname NOT GLOB 'refs/*'
  AND EXISTS (
      SELECT 1 FROM protected_refs full_name
      WHERE full_name.repository_id = protected_refs.repository_id
        AND full_name.refname = 'refs/heads/' || protected_refs.refname
  );

-- quorumgit-statement
UPDATE protected_refs
SET refname = 'refs/heads/' || refname
WHERE refname NOT GLOB 'refs/*';

-- quorumgit-statement
CREATE TRIGGER protected_refs_require_full_name
BEFORE INSERT ON protected_refs
WHEN NEW.refname NOT GLOB 'refs/?*'
BEGIN
    SELECT RAISE(ABORT, 'protected refs must be full ref names such as refs/heads/main');
END;

-- quorumgit-statement
CREATE TRIGGER protected_ref_updates_require_full_name
BEFORE UPDATE OF refname ON protected_refs
WHEN NEW.refname NOT GLOB 'refs/?*'
BEGIN
    SELECT RAISE(ABORT, 'protected refs must be full ref names such as refs/heads/main');
END;
