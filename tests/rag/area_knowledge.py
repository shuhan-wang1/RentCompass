# Filename: local_data_demo/area_knowledge.py
import chromadb

class AreaKnowledgeBase:
    """Store and retrieve curated area information"""
    
    def __init__(self):
        print("    -> [DEBUG] Initializing AreaKnowledgeBase...")
        try:
            print("    -> [DEBUG] Creating ChromaDB client...")
            self.client = chromadb.PersistentClient(path="./chroma_db_area")
            print("    -> [DEBUG] Client created successfully")
            
            print("    -> [DEBUG] Getting/creating collection...")
            self.collection = self.client.get_or_create_collection("area_knowledge")
            print("    -> [DEBUG] Collection ready")
            
            print("    -> [DEBUG] Populating initial data...")
            self._populate_initial_data()
            print("    -> [DEBUG] AreaKnowledgeBase initialized successfully.")
        except Exception as e:
            print(f"    -> ❌ ERROR in AreaKnowledgeBase: {e}")
            raise

    def _populate_initial_data(self):
        """Add curated London area information"""
        areas = [
            {
                "name": "Camden",
                "vibe": "Alternative, vibrant, music scene",
                "demographics": "Young professionals, students, artists",
                "transport": "Northern Line, excellent bus links",
                "safety": "Generally safe, some late-night concerns",
                "prices": "£1800-2500 for 1-bed"
            },
            # Add more areas...
        ]
        
        # Check if data already exists to avoid duplication
        if self.collection.count() == 0:
            for area in areas:
                doc = f"{area['name']}: {area['vibe']}. {area['demographics']}. {area['transport']}. Safety: {area['safety']}."
                self.collection.add(
                    documents=[doc],
                    metadatas=[area],  # Corrected from 'metadatos'
                    ids=[area['name']]
                )
    
    def get_context(self, location: str, n_results: int = 2):
        """Retrieve relevant area information"""
        results = self.collection.query(
            query_texts=[location],
            n_results=n_results
        )
        return results['metadatas'][0] if results['metadatas'] else []