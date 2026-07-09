# property_embeddings.py
from sentence_transformers import SentenceTransformer
import numpy as np
import faiss

class PropertyEmbeddingStore:
    def __init__(self):
        print("    -> [DEBUG] Initializing PropertyEmbeddingStore...")
        # This line downloads or loads the model, which can be a point of failure
        self.model = SentenceTransformer('all-MiniLM-L6-v2')
        self.index = None
        self.properties = []
        print("    -> [DEBUG] PropertyEmbeddingStore initialized successfully.")
    
    def build_index(self, properties: list[dict]):
        """Create FAISS index from property descriptions"""
        if not properties:
            print("    -> [WARNING] No properties provided for indexing!")
            return
        
        texts = []
        for prop in properties:
            # Combine relevant fields into rich text
            address = prop.get('Address', '')
            price = prop.get('Price', '')
            description = prop.get('Description', '')
            
            text = f"{address} {price} {description}"
            texts.append(text.strip())
        
        if not texts:
            print("    -> [WARNING] No valid text to embed!")
            return
        
        print(f"    -> [DEBUG] Encoding {len(texts)} property texts...")
        embeddings = self.model.encode(texts, show_progress_bar=False)
        
        # FAISS index for fast similarity search
        dimension = embeddings.shape[1]
        self.index = faiss.IndexFlatIP(dimension)  # Inner product (cosine)
        faiss.normalize_L2(embeddings)
        self.index.add(embeddings)
        self.properties = properties
        print(f"    -> [DEBUG] FAISS index created with {len(properties)} properties")
    
    def search(self, query: str, top_k: int = 10):
        """Semantic search for properties"""
        if self.index is None:
            print("    -> [WARNING] FAISS index not initialized. Returning empty results.")
            return []
        
        if not query or not isinstance(query, str):
            print("    -> [WARNING] Invalid query provided to search.")
            return []
        
        print(f"    -> [DEBUG] Searching for: {query[:50]}...")
        
        query_embedding = self.model.encode([query])
        faiss.normalize_L2(query_embedding)
        
        scores, indices = self.index.search(query_embedding, min(top_k, len(self.properties)))
        
        results = []
        for idx, score in zip(indices[0], scores[0]):
            if idx < len(self.properties):
                prop = self.properties[idx].copy()
                prop['similarity_score'] = float(score)
                results.append(prop)
        
        print(f"    -> [DEBUG] Found {len(results)} similar properties")
        return results