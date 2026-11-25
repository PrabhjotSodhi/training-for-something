CREATE TABLE phone_pairings (
    secret_hash BLOB PRIMARY KEY NOT NULL,
    -- A code has a deadline. Once redeemed, its phone token lasts until forgotten.
    code_expires_at TEXT
);
