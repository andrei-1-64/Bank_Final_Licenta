-- Server-side conversation persistence for /chat. Until now `history`
-- round-tripped through the client on every request (see chat/router.py);
-- from this migration on, the server owns the transcript and the client
-- only carries a `conversation_id`.
--
-- Se ruleaza dupa 0001/0002/0003/0004/0005, in Supabase SQL Editor.

-- --------------------------------------------------------------------------
-- conversations
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(200)
);
CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations (user_id);

-- --------------------------------------------------------------------------
-- messages (one row per Message turn - see app/ai/schemas.py)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'system')),
    content TEXT,
    tool_calls JSONB,
    tool_call_id VARCHAR(255),
    name VARCHAR(100)
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages (conversation_id);

-- --------------------------------------------------------------------------
-- RLS + grants pentru service_role (vezi nota din 0001).
-- --------------------------------------------------------------------------
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON conversations TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON messages TO service_role;
