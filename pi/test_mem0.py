import os
from mem0 import Memory
import config

os.environ["GOOGLE_API_KEY"] = "test"

# Configuration matches assistant_brains.py exactly
_mem_config = {
    "llm": {
        "provider": "gemini",
        "config": {
            "model": "gemini-1.5-flash",
            "temperature": 0.1,
        }
    },
    "embedder": {
        "provider": "gemini",
        "config": {
            "model": "text-embedding-004", 
        }
    },
    "vector_store": {
        "provider": "chroma",
        "config": {
            "collection_name": "omniorb_memory",
            "path": "/home/pi/assistant/memory_store",
        }
    }
}

try:
    print("Initializing Memory...")
    m = Memory.from_config(_mem_config)
    print("Success!")
except Exception as e:
    print(f"FAILED: {e}")
