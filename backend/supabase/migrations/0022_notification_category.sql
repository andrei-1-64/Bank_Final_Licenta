-- Adds a stable event-category tag to notifications, separate from the
-- free-text title/body (prose, generated in Romanian at creation time - see
-- notifications/service.py::create_notification, which has no locale
-- concept). The frontend needs something more stable than parsing translated
-- prose to decide when to play the special "money received" pop-up
-- animation (see app.js's showNotificationPopup) - NULL for every existing
-- notification and every event this doesn't specifically look for.
--
-- Se ruleaza dupa 0001-0021, in Supabase SQL Editor.

ALTER TABLE notifications ADD COLUMN IF NOT EXISTS category VARCHAR(50);
