# conversation_memory.py
import chromadb
import json

class ConversationMemory:
    def __init__(self):
        print("    -> [DEBUG] Initializing ConversationMemory...")
        self.client = chromadb.PersistentClient(path="./chroma_db")
        
        self.collection = self.client.get_or_create_collection(
            name="conversations",
            metadata={"hnsw:space": "cosine"}
        )
        print("    -> [DEBUG] ConversationMemory initialized successfully.")

    def add_interaction(self, user_msg: str, bot_response: str, 
                       metadata: dict = None):
        """Store conversation turn with metadata"""
        turn_id = f"turn_{self.collection.count()}"
        
        # FIX: Sanitize metadata to only include simple types
        clean_metadata = self._sanitize_metadata(metadata or {})
        
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
    
    def retrieve_relevant_history(self, query: str, n_results: int = 3):
        """Get relevant past conversations"""
        results = self.collection.query(
            query_texts=[query],
            n_results=n_results
        )
        return results['documents'][0] if results['documents'] else []