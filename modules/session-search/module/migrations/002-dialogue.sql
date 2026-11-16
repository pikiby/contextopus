CREATE TABLE session_providers (source TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, root TEXT NOT NULL);
CREATE TABLE session_files (
 path TEXT PRIMARY KEY, source TEXT NOT NULL, session_id TEXT NOT NULL,
 mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL, offset INTEGER NOT NULL,
 inode TEXT NOT NULL, session_json TEXT NOT NULL, pending INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE session_turns (
 id INTEGER PRIMARY KEY, path TEXT NOT NULL, source TEXT NOT NULL,
 session_id TEXT NOT NULL, position INTEGER NOT NULL, role TEXT NOT NULL,
 text TEXT NOT NULL, timestamp TEXT, UNIQUE(path, position)
);
CREATE INDEX session_turns_session ON session_turns(source, session_id, position);
CREATE VIRTUAL TABLE session_fts USING fts5(text, content='session_turns', content_rowid='id');
CREATE TRIGGER session_turns_ai AFTER INSERT ON session_turns BEGIN
 INSERT INTO session_fts(rowid,text) VALUES(new.id,new.text);
END;
CREATE TRIGGER session_turns_ad AFTER DELETE ON session_turns BEGIN
 INSERT INTO session_fts(session_fts,rowid,text) VALUES('delete',old.id,old.text);
END;
