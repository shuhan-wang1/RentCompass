# conversation_memory.py
import chromadb
import json


class ConversationMemory:
    """Semantic store of past conversation turns.

    PRIVACY: every read and write is namespaced by user_id. Retrieval with a
    missing/blank user_id fails CLOSED (returns []) — it must never fall back to
    a global query across all users' turns. Legacy records written before
    namespacing carry no user_id metadata and therefore can never match any
    user-scoped filter (verified Chroma where-semantics); they are left in place
    for the orchestrator to decide on cleanup.
    """

    def __init__(self, db_path: str = "./chroma_db"):
        print("    -> [DEBUG] Initializing ConversationMemory...")
        self.client = chromadb.PersistentClient(path=db_path)

        self.collection = self.client.get_or_create_collection(
            name="conversations",
            metadata={"hnsw:space": "cosine"}
        )
        print("    -> [DEBUG] ConversationMemory initialized successfully.")

    @staticmethod
    def _valid_user_id(user_id):
        if not isinstance(user_id, str):
            return None
        uid = user_id.strip()
        if not uid or uid.lower() == "default":
            return None
        return uid

    def add_interaction(self, user_msg: str, bot_response: str,
                        metadata: dict = None, user_id: str = None):
        """Store conversation turn with metadata. Requires a real user_id."""
        uid = self._valid_user_id(user_id)
        if uid is None:
            print("    -> [ConversationMemory] add_interaction rejected: missing user_id")
            return
        turn_id = f"turn_{uid}_{self.collection.count()}"

        # FIX: Sanitize metadata to only include simple types
        clean_metadata = self._sanitize_metadata(metadata or {})
        clean_metadata["user_id"] = uid

        self.collection.add(
            documents=[f"User: {user_msg}\nAssistant: {bot_response}"],
            metadatas=[clean_metadata],
            ids=[turn_id]
        )

    def _sanitize_metadata(self, metadata: dict) -> dict:
        """Convert complex metadata to ChromaDB-compatible format"""
        clean = {}

        for key, value in metadata.items():
            if value is None:
                continue
            elif isinstance(value, (str, int, float, bool)):
                clean[key] = value
            elif isinstance(value, list):
                # Convert lists to comma-separated strings
                clean[key] = ", ".join(str(item) for item in value) if value else ""
            elif isinstance(value, dict):
                # Convert dicts to JSON strings
                clean[key] = json.dumps(value)
            else:
                # Convert anything else to string
                clean[key] = str(value)

        return clean

    def retrieve_relevant_history(self, query: str, n_results: int = 3,
                                  user_id: str = None):
        """Get relevant past conversations — STRICTLY scoped to one user.

        No user_id -> [] (fail closed). Never queries globally.
        """
        uid = self._valid_user_id(user_id)
        if uid is None:
            return []
        try:
            results = self.collection.query(
                query_texts=[query],
                n_results=n_results,
                where={"user_id": uid},
            )
        except Exception:
            return []
        return results['documents'][0] if results['documents'] else []
