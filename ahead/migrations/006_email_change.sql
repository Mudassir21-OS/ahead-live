-- Phase 3c: a new email address only replaces the old one after it is confirmed (link sent to the new address).
-- The address waiting for confirmation is kept on the one-time link itself.
ALTER TABLE account_tokens ADD COLUMN new_email TEXT;
